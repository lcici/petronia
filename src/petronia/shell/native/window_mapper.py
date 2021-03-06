
"""
Ties the native OS window handle (hwnd) to an internal ID.
Also keeps track of the window information.

This needs to be one of the last components created, because it
sends out events at creation time that other components will want.
"""

from ...system import event_ids
from ...system import target_ids
from ...system.component import Component, Identifiable
from ...system.id_manager import IdManager
from ...config import Config
from ...arch.windows_constants import PETRONIA_CREATED_WINDOW__CLASS_PREFIX
from ...arch.funcs import (
    window__find_handles, window__get_style, window__set_style, window__border_rectangle,
    window__redraw, window__get_process_id, process__get_username_domain_for_pid,
    window__get_module_filename, process__get_executable_filename,
    window__get_class_name, window__is_visible, window__set_position,
    window__activate, window__get_title, window__get_visibility_states,
    window__get_active_window, window__maximize, window__minimize, window__move_resize,
    window__restore,
    process__get_current_pid, process__get_username_domain_for_pid,
    shell__set_window_metrics
)
import atexit

_CURRENT_PROCESS_ID = process__get_current_pid()
_CURRENT_USER_DOMAIN = process__get_username_domain_for_pid(_CURRENT_PROCESS_ID)


class WindowMapper(Identifiable, Component):
    def __init__(self, bus, id_manager, config):
        Component.__init__(self, bus)
        Identifiable.__init__(self, target_ids.WINDOW_MAPPER)
        assert isinstance(id_manager, IdManager)
        assert isinstance(config, Config)
        self.__id_manager = id_manager
        self.__config = config

        # Pre-populate existing windows

        hwnd_list = window__find_handles()
        self.__handle_map = {}
        self.__cid_to_handle = {}
        self.__hwnd_restore_state = {}
        for hwnd in hwnd_list:
            try:
                self._init_window(hwnd)
            except BaseException as e:
                self._log_error("WindowMapper failed to initialize window {0}".format(hwnd), e)
        self._log_verbose("===== Finished existing window registration =====")

        self._listen(event_ids.OS__WINDOW_CREATED, target_ids.ANY, self._on_window_created)
        self._listen(event_ids.OS__WINDOW_DESTROYED, target_ids.ANY, self._on_window_destroyed)
        self._listen(event_ids.OS__WINDOW_FOCUSED, target_ids.ANY, self._on_window_focused)
        # self._listen(event_ids.OS__SHELL_WINDOW_FOCUSED, target_ids.ANY, None)
        self._listen(event_ids.OS__WINDOW_MINIMIZED, target_ids.ANY, self._on_window_minimized)
        self._listen(event_ids.OS__WINDOW_REDRAW, target_ids.ANY, self._on_window_redraw)
        # self._listen(event_ids.OS__TASK_MANAGER_FOCUSED, target_ids.ANY, None)
        # self._listen(event_ids.OS__LANGUAGE, target_ids.ANY, None)
        # self._listen(event_ids.OS__SYS_MENU, target_ids.ANY, None)
        self._listen(event_ids.OS__WINDOW_FORCED_END, target_ids.ANY, self._on_window_forced_end)

        # These two occur when a window comes back from being hung
        self._listen(event_ids.OS__WINDOW_REPLACING, target_ids.ANY, self._on_window_replacing)
        self._listen(event_ids.OS__WINDOW_REPLACED, target_ids.ANY, self._on_window_replaced)

        # self._listen(event_ids.OS__WINDOW_MONITOR_CHANGED, target_ids.ANY, None)
        self._listen(event_ids.OS__WINDOW_FLASH, target_ids.ANY, self._on_window_flash)
        # self._listen(event_ids.OS__APP_COMMAND, target_ids.ANY, None)

        # Events from the system that request OS actions.
        self._listen(event_ids.LAYOUT__SET_RECTANGLE, target_ids.ANY, self._on_window_move_resize)
        self._listen(event_ids.TELL_WINDOWS__FOCUS_WINDOW, target_ids.ANY, self._on_set_window_focus)
        self._listen(event_ids.ZORDER__SET_WINDOW_ON_TOP, target_ids.ANY, self._on_set_window_top)
        self._listen(event_ids.TELL_WINDOWS__MINIMIZE_WINDOW, target_ids.ANY, self._on_minimize_window)
        self._listen(event_ids.TELL_WINDOWS__MAXIMIZE_WINDOW, target_ids.ANY, self._on_maximize_window)
        self._listen(event_ids.TELL_WINDOWS__RESIZE_WINDOW, target_ids.ANY, self._on_window_resize)

        # Requests that require knowing OS states of windows
        self._listen(event_ids.FOCUS__MAKE_OWNED_PORTAL_ACTIVE, target_ids.WINDOW_MAPPER,
                     self._on_make_owned_portal_active)

        self._listen(event_ids.LAYOUT__RESEND_WINDOW_CREATED_EVENTS, target_ids.ANY,
                     self._on_resend_window_created_events)

    def close(self):
        try:
            for hwnd, state in self.__hwnd_restore_state.items():
                _restore_window_state(hwnd, state[0], state[1])
        finally:
            super().close()

    def _setup_window_style(self, info):
        if 'title' not in info:
            info['title'] = window__get_title(info['hwnd'])
        is_managed, remove_border, remove_title = self._get_managed_chrome_details(info)
        if is_managed:
            # print("DEBUG managed border {0}, title {1} for {2}".format(remove_border, remove_title, info))
            hwnd = info['hwnd']
            orig_size = window__border_rectangle(hwnd)
            orig_style = window__get_style(hwnd)
            self.__hwnd_restore_state[hwnd] = (orig_size, orig_style)
            # Always, always restore window state at exit.  This ensures it.
            atexit.register(_restore_window_state, hwnd, orig_size, orig_style)
            style_data = {}
            if remove_title:
                style_data['border'] = False
                style_data['dialog-frame'] = False
            if remove_border:
                style_data['size-border'] = False
            if len(style_data) > 0:
                try:
                    window__set_style(hwnd, style_data)
                except OSError as e:
                    self._log_debug("Problem setting style for {0}".format(info['class']), e)
                window__redraw(hwnd)

    def _init_window(self, hwnd):
        pid = window__get_process_id(hwnd)
        if _CURRENT_PROCESS_ID == pid:
            return None
        class_name = window__get_class_name(hwnd)
        try:
            username_domain = process__get_username_domain_for_pid(pid)
            self._log_debug("window {0}, pid {1}, owned by [{2}@{3}]".format(
                hwnd, pid, username_domain[0], username_domain[1]))
        except OSError as e:
            # Most probably an access problem.  We don't want to manage programs
            # that we can't access.
            self._log_debug("username/domain read problem for window {0}, pid {1}, class {2}".format(
                hwnd, pid, class_name), e)
            username_domain = ("[aborted]", "[aborted]")
            # return None
        # Only manage windows that the user owns.
        if username_domain != _CURRENT_USER_DOMAIN:
            self._log_debug("ignoring window with pid {0}, class {1} from other user {2}@{3}".format(
                pid, class_name, username_domain[0], username_domain[1])
            )
            if class_name == 'PuTTY':
                print("PUTTY ignoring window; detected {0}\\{1}, have {2}\\{3}".format(
                    username_domain[1], username_domain[0], _CURRENT_USER_DOMAIN[1], _CURRENT_USER_DOMAIN[0]
                ))
            return None
        if class_name is None or class_name.startswith(PETRONIA_CREATED_WINDOW__CLASS_PREFIX):
            self._log_debug("Ignoring self-managed window with class {0}".format(class_name))
            return None
        cid = self.__id_manager.allocate('hwnd')
        key = str(hwnd)
        module_filename = ""
        try:
            module_filename = window__get_module_filename(hwnd)
        except OSError as e:
            self._log_debug("Ignoring problem from window__get_module_filename", e)
        if module_filename is None:
            module_filename = ''
        exec_filename = ""
        try:
            exec_filename = process__get_executable_filename(pid)
        except OSError as e:
            self._log_debug("Ignoring problem from process__get_executable_filename", e)
        if exec_filename is None:
            exec_filename = ''
        visible = window__is_visible(hwnd)
        if visible:
            info = {
                'cid': cid,
                'hwnd': hwnd,
                'class': class_name,
                'module_filename': module_filename,
                'exec_filename': exec_filename,
                'pid': pid,
                'visible': visible,
            }
            self._setup_window_style(info)
            self.__handle_map[key] = info
            self.__cid_to_handle[cid] = hwnd
            self._log_debug("Registered {0} ({1}) ({2}) ({3}) as {4}".format(
                hex(hwnd), module_filename, exec_filename, pid, cid))
            if self._is_tile_managed(info):
                self._fire_for_window(event_ids.WINDOW__CREATED, info)
            else:
                self._fire(
                    event_ids.LAYOUT__WINDOW_PUT_OUTSIDE_MANAGEMENT,
                    target_ids.UNOWNED_WINDOW_PORTAL,
                    {
                        'window-cid': cid,
                        'window-info': self._create_window_info(info)
                    }
                )
            return info
        return None

    def _get_managed_chrome_details(self, window_info):
        """

        :param window_info:
        :return: (should be managed chrome, remove border?, remove title?)
        """
        if window_info['visible'] and not self.__config.shell.matches_shell_window(window_info):
            has_title = self.__config.applications.has_title(window_info)
            has_border = self.__config.applications.has_border(window_info)
            # print("DEBUG window managed as border {0} title {1}: {2}".format(has_border, has_title, window_info))
            return not (has_title and has_border), not has_border, not has_title
        # print("DEBUG window not visible or is shell: {0}".format(window_info))
        return False, False, False

    def _is_tile_managed(self, window_info):
        return (
            window_info['visible']
            and self.__config.applications.is_tiled(window_info)
            and not self.__config.shell.matches_shell_window(window_info)
        )

    # noinspection PyUnusedLocal
    def _on_window_created(self, event_id, target_id, obj):
        hwnd = obj['target_hwnd']
        self._init_window(hwnd)

    # noinspection PyUnusedLocal
    def _on_window_destroyed(self, event_id, target_id, obj):
        hwnd = obj['target_hwnd']
        key = str(hwnd)
        # No need to check if the window is registered
        if key in self.__handle_map:
            info = self.__handle_map[key]
            self._fire_for_window(event_ids.WINDOW__CLOSED, info)
            # This can cause a double delete.
            if key in self.__handle_map:
                del self.__handle_map[key]
            if info['cid'] in self.__cid_to_handle:
                del self.__cid_to_handle[info['cid']]
            if hwnd in self.__hwnd_restore_state:
                del self.__hwnd_restore_state[hwnd]

    # noinspection PyUnusedLocal
    def _on_window_focused(self, event_id, target_id, obj):
        if 'target_hwnd' in obj:
            hwnd = obj['target_hwnd']
        elif 'source_hwnd' in obj:
            # TODO this should be handled better in the generating event.
            hwnd = obj['source_hwnd']
        else:
            return
        key = str(hwnd)
        if key in self.__handle_map:
            info = self.__handle_map[key]
            self._fire_for_window(event_ids.WINDOW__FOCUSED, info)

    # noinspection PyUnusedLocal
    def _on_window_minimized(self, event_id, target_id, obj):
        hwnd = obj['target_hwnd']
        key = str(hwnd)
        if key in self.__handle_map:
            info = self.__handle_map[key]
            # TODO do something

    # noinspection PyUnusedLocal
    def _on_window_redraw(self, event_id, target_id, obj):
        hwnd = obj['target_hwnd']
        key = str(hwnd)
        if key in self.__handle_map:
            info = self.__handle_map[key]
            self._fire_for_window(event_ids.WINDOW__REDRAW, info)

    # noinspection PyUnusedLocal
    def _on_window_forced_end(self, event_id, target_id, obj):
        hwnd = obj['target_hwnd']
        self._on_window_destroyed(event_id, target_id, obj)

    # noinspection PyUnusedLocal
    def _on_window_replacing(self, event_id, target_id, obj):
        hwnd = obj['target_hwnd']
        key = str(hwnd)
        if key in self.__handle_map:
            info = self.__handle_map[key]
            # TODO do something?  Don't think there's anything to do here.

    # noinspection PyUnusedLocal
    def _on_window_replaced(self, event_id, target_id, obj):
        hwnd = obj['target_hwnd']
        key = str(hwnd)
        if key in self.__handle_map:
            info = self.__handle_map[key]
            # TODO do something
            # Here, it looks like this means one handle is replaced
            # with a different handle.  This should swap out the
            # internal hwnd info object.  Looks like we need more
            # information - the 2 handles, the original and the
            # new one.

    # noinspection PyUnusedLocal
    def _on_window_flash(self, event_id, target_id, obj):
        hwnd = obj['target_hwnd']
        key = str(hwnd)
        if key in self.__handle_map:
            info = self.__handle_map[key]
            self._fire_for_window(event_ids.WINDOW__FLASHING, info)

    # noinspection PyUnusedLocal
    def _on_window_move_resize(self, event_id, target_id, obj):
        if target_id in self.__cid_to_handle:
            hwnd = self.__cid_to_handle[target_id]
            info = None
            resize = True
            if str(hwnd) in self.__handle_map:
                info = self._create_window_info(self.__handle_map[str(hwnd)])
            if 'x' in obj and 'y' in obj and 'height' in obj and 'width' in obj:
                # Move and resize the window and possibly make it on top
                # of all the other windows.
                if not _move_resize_window(
                            hwnd, info, self.__config,
                            int(obj['x']), int(obj['y']), int(obj['width']), int(obj['height']),
                            obj
                        ):
                    self._on_window_destroyed(event_id, target_id, {'target_hwnd': hwnd})
                return

            # Could not move or resize, so just send it to the top if necessary.
            if 'make-focused' in obj and obj['make-focused']:
                if not window__activate(hwnd):
                    self._on_window_destroyed(event_id, target_id, {'target_hwnd': hwnd})

    # noinspection PyUnusedLocal
    def _on_set_window_focus(self, event_id, target_id, obj):
        if target_id in self.__cid_to_handle:
            hwnd = self.__cid_to_handle[target_id]

            if not str(hwnd) in self.__handle_map:
                self._log_error("Attempting to set focus on unknown handle {0}".format(info))
                return
            info = self.__handle_map[str(hwnd)]
            info = self._create_window_info(info)
            self._log_debug("Making window active: {0}".format(info))

            if not 'restored' in info['visibility']:
                # Whether the window is maximized or restored, change it.
                window__restore(hwnd)

            if window__activate(hwnd):
                self._fire_for_window(event_ids.WINDOW__FOCUSED, self.__handle_map[str(hwnd)])
            else:
                self._log_info("Attempted to focus on a window that isn't responsive ({0} / {1})".format(
                    target_id, hwnd))
                self._on_window_destroyed(None, None, {'target_hwnd': hwnd})
        else:
            self._log_info("Could not run {0}; no such window id {1}".format(event_id, target_id))

    # noinspection PyUnusedLocal
    def _on_set_window_top(self, event_id, target_id, obj):
        self._on_set_window_focus(event_id, target_id, obj)

    # noinspection PyUnusedLocal
    def _on_maximize_window(self, event_id, target_id, obj):
        if target_id in self.__cid_to_handle:
            hwnd = self.__cid_to_handle[target_id]
        else:
            hwnd = window__get_active_window()

        if hwnd:
            if not window__maximize(hwnd):
                self._log_info("Attempted to focus on a window that isn't responsive ({0} / {1})".format(
                    target_id, hwnd))
                self._on_window_destroyed(None, None, {'target_hwnd': hwnd})

    # noinspection PyUnusedLocal
    def _on_minimize_window(self, event_id, target_id, obj):
        if target_id in self.__cid_to_handle:
            print("Using a known window handle")
            hwnd = self.__cid_to_handle[target_id]
        else:
            print("getting the active window handle")
            hwnd = window__get_active_window()

        if hwnd is not None:
            # print("DEBUG minimizing handle " + hwnd)
            if not window__minimize(hwnd):
                self._log_info("Attempted to focus on a window that isn't responsive ({0} / {1})".format(
                    target_id, hwnd))
                self._on_window_destroyed(None, None, {'target_hwnd': hwnd})
        # else:
        #     print("DEBUG nothing to minimize")

    # noinspection PyUnusedLocal
    def _on_window_resize(self, event_id, target_id, obj):
        if target_id in self.__cid_to_handle:
            print("Using a known window handle")
            hwnd = self.__cid_to_handle[target_id]
        else:
            print("getting the active window handle")
            hwnd = window__get_active_window()

        if hwnd is not None:
            # print("DEBUG minimizing handle " + hwnd)
            rect = window__border_rectangle(hwnd)
            width = rect['width'] + int(obj['adjust-x'])
            height = rect['height'] + int(obj['adjust-y'])
            print("Resizing to ({0}, {1}), {2}x{3}".format(rect['x'], rect['y'], width, height))
            window__move_resize(hwnd, rect['x'], rect['y'], width, height)
        else:
            self._log_warn("No active window found")

    # noinspection PyUnusedLocal
    def _on_make_owned_portal_active(self, event_id, target_id, obj):
        focused_window_hwnd = window__get_active_window()
        key = str(focused_window_hwnd)
        if key in self.__handle_map:
            info = self.__handle_map[key]
            # By sending the window__focused event for the window cid, the owned
            # portal activates itself
            self._fire_for_window(event_ids.WINDOW__FOCUSED, info)
        else:
            self._log_warn("Could not find window CID that is active; handle is {0}".format(key))

    # noinspection PyUnusedLocal
    def _on_resend_window_created_events(self, event_id, target_id, obj):
        self._log_debug("Resending window create events.")
        for info in self.__handle_map.values():
            if self._is_tile_managed(info):
                self._fire_for_window(event_ids.WINDOW__CREATED, info)

    def _fire_for_window(self, event_id, info):
        # Only fire for visible windows
        if info['visible']:
            full_info = self._create_window_info(info)
            self._fire(event_id, info['cid'], {
                'window-cid': info['cid'],
                'window-info': full_info,
            })

    @staticmethod
    def _create_window_info(info):
        if info['visible']:
            hwnd = info['hwnd']
            try:
                title = window__get_title(hwnd)
                rect = window__border_rectangle(hwnd)
                visibility = window__get_visibility_states(hwnd)
            except OSError:
                # Window is gone now (#4)
                title = ""
                rect = {'x': 0, 'y': 0, 'width': 0, 'height': 0, 'top': 0, 'bottom': 0, 'left': 0, 'right': 0}
                visibility = {}
            full_info = {
                # Some things we load every time.
                'cid': info['cid'],
                'title': title,
                'border': rect,
                'visibility': visibility,

                # Others are static
                'hwnd': hwnd,
                'class': info['class'],
                'module_filename': info['module_filename'],
                'exec_filename': info['exec_filename'],
                'pid': info['pid'],
                'visible': info['visible'],
            }
            return full_info
        return info


def _restore_window_state(hwnd, size, style):
    try:
        window__set_style(hwnd, style)
    except OSError:
        pass

    # Set position to redraw the window AFTER setting the style.
    try:
        window__set_position(
            hwnd, None, size['x'], size['y'], size['width'], size['height'],
            ["frame-changed", "no-zorder", "async-window-pos"])
    except OSError:
        pass


def _move_resize_window(hwnd, window_info, config, pos_x, pos_y, width, height, options):
    do_resize = window_info is None or config.applications.is_resizable(window_info)

    # Because we check the final size of the window, we don't use "async"
    flags = ['frame-changed', 'draw-frame']
    if not do_resize:
        # print("DEBUG do not resize window (info is {0})".format(window_info is None and 'None' or 'set'))
        flags.append("no-size")
        flags.append('async-window-pos')

    z_order = None
    if 'make-focused' in options and options['make-focused']:
        z_order = 'topmost'
    if do_resize:
        if not window__set_position(hwnd, z_order, pos_x, pos_y, width, height, flags):
            return False

    try:
        final_size = window__border_rectangle(hwnd)
    except OSError:
        return False

    if not do_resize or final_size['width'] != width or final_size['height'] != height:
        # print("DEBUG requested size {0}x{1}, found {2}x{3}".format(
        #     width, height, final_size['width'], final_size['height']))

        # The window could not be inserted into the portal at the expected size.
        # Put the window in according to the position options.
        v = ('v-snap' in options and options['v-snap'] or 'top').strip().lower()
        h = ('h-snap' in options and options['h-snap'] or 'left').strip().lower()
        if v == 'bottom':
            y = pos_y + height - final_size['height']
        elif v == 'center':
            y = pos_y + (height // 2) - (final_size['height'] // 2)
        else:
            y = pos_y
        if h == 'right':
            x = pos_x + width - final_size['width']
        elif h == 'center':
            x = pos_x + (width // 2) - (final_size['width'] // 2)
        else:
            x = pos_x

        flags.append("no-size")
        flags.append('async-window-pos')

        # print("DEBUG could not fit window into portal, snapping {2} {3} at ({0},{1})".format(x, y, v, h))
        if not window__set_position(hwnd, z_order, x, y, 0, 0, flags):
            return False

    # if 'make-focused' in obj and obj['make-focused']:
    #     if not window__activate(hwnd):
    #         return False
    return True

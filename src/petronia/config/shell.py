
from .base_config import BaseConfig


class ShellConfig(BaseConfig):
    """
    Configures usage of the shell.

    The Windows shell is kind of like the x Window Manager.
    """
    def __init__(self):
        super().__init__()

    def get_shell_component_factory(self):
        """
        Returns the factory function that constructs the
        ShellType component.

        :return: a singleton factory for ShellType objects.
        """
        raise NotImplementedError()

    def get_taskbar_position(self):
        """
        Returns the normal position of the task bar, as it should be used by
        the window arranger to not cover up the window.  If you want the
        task bar to allow windows to cover it, then this should return an empty
        box.

        :return: dictionary with the keys 'top', 'bottom', 'left', 'right'.
        """
        raise NotImplementedError()

    def matches_shell_window(self, window_info):
        """
        Checks if the given window is considered a "shell" window, and thus
        should not be affected by Tiling.

        :param window_info:
        :return: True if the window information describes a shell-specific window,
            or False if it does not.
        """
        raise NotImplementedError()

    @property
    def show_taskbar_with_start_menu(self):
        """

        :return: True if the task bar is shown whenever the user requests the start menu to be shown;
            False if default OS behavior is used.
        """
        return True


class WindowsShellConfig(ShellConfig):
    """
    Default Windows Explorer shell.
    """
    def __init__(self, border_width=6, border_color=0xffee00):
        super().__init__()
        self.border_width = border_width
        self.border_padding = 0
        self.border_color = border_color
        self.scrollbar_width = 0
        self.scrollbar_height = 0

    def get_shell_component_factory(self):
        return shell_explorer_factory

    def get_taskbar_position(self):
        """
        If your task-bar is in "auto-hide" mode,

        :return:
        """
        from ..shell.native.shell_explorer import get_taskbar_position as get_pos
        return get_pos()

    def matches_shell_window(self, window_info):
        # Explorer.exe commands,
        # which are NOT file navigation windows.
        # This seems very Windows version specific...
        return (
            window_info['exec_filename'].lower().endswith('\\explorer.exe')
            and window_info['class'] != 'CabinetWClass'
        )

    def get_system_window_settings(self):
        # see shell__set_window_metrics
        ret = {}
        if self.border_width > 0:
            ret['border-width'] = self.border_width
        if self.border_padding >= 0:
            ret['padded-border-width'] = self.border_padding
        if self.scrollbar_width > 0:
            ret['scroll-width'] = self.scrollbar_width
        if self.scrollbar_height > 0:
            ret['scroll-height'] = self.scrollbar_height
        # All other chrome is ignored for this call
        return ret


# noinspection PyUnusedLocal
def shell_explorer_factory(bus, config, id_manager):
    from ..shell.native.shell_explorer import ShellExplorer

    return ShellExplorer(bus, config)

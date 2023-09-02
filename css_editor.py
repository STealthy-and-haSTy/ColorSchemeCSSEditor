import sublime
import sublime_plugin

from os.path import basename, splitext
from bisect import bisect


## ----------------------------------------------------------------------------


# When we mark regions in the source color scheme file that represent CSS that
# is being edited, these values are used to set the icon, scope and flags on
# the added regions.
CSS_REGION_INFO = {
    'scope': 'region.bluish',
    'flags': sublime.DRAW_SOLID_UNDERLINE | sublime.DRAW_NO_FILL |
             sublime.DRAW_NO_OUTLINE | sublime.PERSISTENT,
    'icon':  'Packages/ColorSchemeCSSEditor/icons/file_type_css.png'
}

# The types of CSS that can appear in a color scheme; this should be the names
# of all available CSS keys in the color scheme that we would like the package
# to be able to edit
CSS_TYPES = ('popup_css', 'phantom_css', 'sheet_css')

# In views that we create to contain the expanded CSS text for editing, these
# settings keys are used to track the "type" of CSS that the buffer is holding
# and the name of open file that it came from. The first setting listed here is
# a boolean that flags whether or not a view is a SubCSS tab.
#
# The valid types of CSS are in the CSS_TYPES list.
SUBCSS_TAB="subcss_tab"
SUBCSS_TYPE="subcss_type"
SUBCSS_SOURCE="subcss_source"

# When a SubCSS region is used to construct a new tab for editing purposes, the
# part of the source file that was split out needs to be marked with a region
# so that we know where to insert changes back. There can be many of these per
# file, so we need multiple regions to keep them distinct.
SUBCSS_REGION = lambda css_type: f"sub_{css_type}"


## ----------------------------------------------------------------------------


def plugin_loaded():
    """
    Initialize plugin state.
    """
    cs_setting.obj = sublime.load_settings("ColorSchemeCSSEditor.sublime-settings")
    cs_setting.default = {
        "open_as_split": True,
        "save_on_update": True,
        "update_on_close": True,
        "css_syntax": "Packages/CSS/CSS.sublime-syntax"
    }


def cs_setting(key):
    """
    Get a package setting from a cached settings object, returning the most
    sensible default if that setting is not set.
    """
    default = cs_setting.default.get(key, None)
    return cs_setting.obj.get(key, default)


## ----------------------------------------------------------------------------


def find_css_view(source_view, css_type):
    """
    Given a view that is the view from which the CSS content in a SubCSS view
    was extracted, find the associated SubCSS view that has the given
    css_type and return it.

    If there is no such view, this will return None.
    """
    for window in sublime.windows():
        for view in window.views():
            # If this view has the correct name, and the name of the file that
            # is open in it is the source view's file name, this view is the
            # css view that associates with the source.
            if view.settings().get(SUBCSS_TYPE) == css_type:
                if source_view.file_name() == view.settings().get(SUBCSS_SOURCE):
                    return view

    return None


def find_source_view(sub_view):
    """
    Given a view that is a SubCSS view, find the view that was the source of
    the CSS used to populate the window, and return it.

    If there is no such view, this will return None.
    """
    target_filename = sub_view.settings().get(SUBCSS_SOURCE)
    for window in sublime.windows():
        for view in window.views():
            # If this view has a file in it and the name of that file is the
            # file that is the marked source for our view, we have found the
            # parent source view.
            if view.file_name() is not None:
                if view.file_name() == target_filename:
                    return view

    return None


## ----------------------------------------------------------------------------


class EditColorSchemeCssCommand(sublime_plugin.TextCommand):
    """
    Given a color scheme CSS settings key which is one of the key outlined  in
    CSS_TYPES, check the curent view to see if it's an editable color scheme
    file that has that settings key in it.

    If it does, and there is not already an editor for that kind of CSS open
    already, this will open a scratch CSS view that contains the CSS content of
    the given key type to allow for editing.
    """
    def run(self, edit, css_type):
        file_base_name = splitext(basename(self.view.file_name()))[0]

        # Get the region that spans the CSS type that the user wants to edit,
        # and the text within it.
        css_region = self.get_css_content(css_type)
        css_data = self.view.substr(css_region)

        # Decode the extracted value to get the actual content; this will strip
        # away the double quotes since it is being treated as a JSON string.
        try:
            css_data = sublime.decode_value(css_data)
        except:
            return self.view.window().status_message(f'Unable to decode CSS for {css_type}')

        # Mark the region that we extracted with the CSS information.
        self.view.add_regions(SUBCSS_REGION(css_type), [css_region], **CSS_REGION_INFO)

        # Create a new temporary CSS view in the current window; this should
        # have a distinguishing name, be set as a scratch buffer, and have as
        # content the extracted css_data.
        flags = sublime.ADD_TO_SELECTION if cs_setting("open_as_split") else 0
        temp = self.view.window().new_file(flags=flags , syntax=cs_setting("css_syntax"))
        temp.run_command("append", {"characters": css_data})
        temp.set_scratch(True)
        temp.set_name(f"Inline CSS: {file_base_name} {css_type}")

        # Set the subcss tab settings so that it knows that it's a subtab and
        # what source file spawned it.
        temp.settings().set(SUBCSS_TAB, True)
        temp.settings().set(SUBCSS_TYPE, css_type)
        temp.settings().set(SUBCSS_SOURCE, self.view.file_name())


    def is_enabled(self, css_type):
        # If the css type given is not valid, or this is not a color scheme
        # file the user can edit, we can't be enabled.
        if css_type not in CSS_TYPES or not self.is_editable_color_scheme():
            return False

        # If this view already has an opened tab for the type of CSS that is
        # requested, we cannot open another
        if bool(self.view.get_regions(SUBCSS_REGION(css_type))):
            return False

        # If this color scheme file has the appropriate key, then we can do the
        # edit.
        return self.has_css_key(css_type) is not None


    def is_visible(self, css_type):
        return self.is_editable_color_scheme()


    def get_css_content(self, css_type):
        """
        Return a sublime.Region that covers the content of the color scheme
        CSS key that has been provided. This region will cover the double
        quotes that wrap the value.

        This assumes that the css_type exists in the file and does not handle
        cases where it does not.
        """
        # Get the region that is wrapping the key that marks this CSS type in
        # the current file; then find a list of all of the regions that mark
        # out the values for keys inside of top level objects (like "globals").
        key = self.has_css_key(css_type)
        values = self.view.find_by_selector('meta.mapping.value meta.mapping.value')

        # Collect the start point for all of the values and find the position
        # in the list that comes after the key we know is present to get the
        # scoped region that outlines the value for that key.
        v_lines = [p.begin() for p in values]
        return values[bisect(v_lines, key.a)]


    def has_css_key(self, css_type):
        """
        Checks the current view, which is assumed to be a color scheme, to see
        if it has a global override key for the css type provided.

        The return value is the region that outlines the key for that type of
        CSS on success, or None if it does not eist.
        """
        keys = self.view.find_by_selector('meta.mapping.key')
        value = f'"{css_type}"'
        for key in keys:
            if self.view.substr(key) == value:
                return key

        return None

    def is_editable_color_scheme(self):
        """
        Check to see if the view associated with the command is an editable
        color scheme file or not.
        """
        # Never enable ourselves in a read only view or if the kind of CSS the
        # user wants to edit is one we don't know about.
        if self.view.is_read_only():
            return False

        # If this view has no filename, or it's not a color scheme we can't
        # edit.
        name = self.view.file_name()
        if name is None or not name.endswith('.sublime-color-scheme'):
            return False

        return True


class FocusSubCssParentCommand(sublime_plugin.TextCommand):
    """
    Determines what view represents the parent view of a SubCSS view (the file
    from which the CSS was originally gathered and will be saved to). That view
    will be focused.
    """
    def run(self, edit):
        source_view = find_source_view(self.view)
        if not source_view:
            return

        source_view.window().focus_view(source_view)

    def is_visible(self):
        s = self.view.settings()
        return s.get(SUBCSS_TAB, False) and s.has(SUBCSS_TYPE)


    is_enabled = is_visible


class SaveSubCssSourceCommand(sublime_plugin.TextCommand):
    """
    This command will only enable itself in a view that is a SubCSS view; in
    such a case, the content of the file will be packaged up and sent via a
    call to a command that will replace the marked CSS region with the new
    CSS string.
    """
    def run(self, edit):
        # Get the type of CSS that this view contains
        css_type = self.view.settings().get(SUBCSS_TYPE)

        # Find the view from which our CSS came; if there is not one, then we
        # must bail with an error (but this should never happen).
        source_view = find_source_view(self.view)
        if not source_view:
            return self.view.window().status_message("Unable to find source file")

        # If whitespace trimming on save is turned on, then trim our content
        # before we shift it over.
        if self.view.settings().get("trim_trailing_white_space_on_save") != "none":
            self.view.run_command("trim_trailing_white_space")

        # Grab the entire content of the buffer and encoded it as a JSON string=
        data = self.view.substr(sublime.Region(0, len(self.view)))
        data = sublime.encode_value(data)

        # Invoke the command that will update the CSS source in the main view
        # with the newly edited content.
        source_view.run_command("do_sub_css_replace", {
            "css": data,
            "css_type": css_type
        })

    def is_enabled(self):
        s = self.view.settings()
        return s.get(SUBCSS_TAB, False) and s.has(SUBCSS_TYPE)


class DoSubCssReplaceCommand(sublime_plugin.TextCommand):
    """
    This command accepts a string of CSS data, which it will attempt to insert
    into the view within the subcss marked region; if there is no such region
    this will do nothing.

    This command is intended for internal package use only.
    """
    def run(self, edit, css, css_type):
        # Find the region into which the CSS should be inserted; this should be
        # a single region.
        source = self.view.get_regions(SUBCSS_REGION(css_type))
        if len(source) != 1:
            return self.view.window().status_message("Unable to find the css source region to replace")

        # The region we want to insert into is the first (and only) region that
        # we found; calculate the difference in size between the content we
        # will insert and the size of the region as it currently exists.
        region = source[0]
        diff = len(css) - len(region)

        # Update the region with the new content.
        self.view.replace(edit, region, css)
        if cs_setting("save_on_update"):
            sublime.set_timeout(lambda: self.view.run_command('save'))

        # If the length of the new CSS is smaller than the existing region,
        # the difference in size will be positive and so we need to shrink the
        # region down.
        if diff > 0:
            region = sublime.Region(region.a, region.b + diff)
            self.view.add_regions(SUBCSS_REGION(css_type), [region], **CSS_REGION_INFO)


## ----------------------------------------------------------------------------


class OverallCssListener(sublime_plugin.EventListener):
    """
    This listens for views that are about to close; if the view being closed is
    the source of a CSS subtab, then the CSS subtab will also be closed.

    Also, if the view being closed is a CSS subtab, then the handler will
    remove the CSS markup regions from the file to let the user know that those
    sections are no longer being edited.
    """
    def close_subcss_views(self, view):
        """
        If this view is a view that has any SubCSS tabs open on its content,
        find all of those sub views and close them.
        """
        for css_type in CSS_TYPES:
            if view.get_regions(SUBCSS_REGION(css_type)):
                sub_view = find_css_view(view, css_type)
                if sub_view is not None:
                    sub_view.close()


    def on_pre_close(self, view):
        # If this view contains any regions that mark it as the source CSS for
        # a subtab, then find the subtab and close it.
        self.close_subcss_views(view)

        # If this view has the setting that marks it as a subcss tab, then find
        # the associated view that the CSS came from and remove the source CSS
        # regions from it.
        if view.settings().get(SUBCSS_SOURCE, False):
            if cs_setting("update_on_close"):
                view.run_command('save_sub_css_source')
            css_type = view.settings().get(SUBCSS_TYPE)
            main_view = find_source_view(view)
            if main_view is not None:
                main_view.erase_regions(SUBCSS_REGION(css_type))


    def click(self, view, css_type):
        sub_view = find_css_view(view, css_type)
        if sub_view is not None:
            sub_view.window().focus_view(sub_view)


    def on_hover(self, view, point, hover_zone):
        # We only want to allow hovers over the gutter
        if hover_zone != sublime.HOVER_GUTTER:
            return

        # We only want to support hovers in the gutter for files that have a
        # SubCSS tab open somewhere; grab out all of the regions that delineate
        # that, since we need to know which one the hover is at.
        css_regions = [view.get_regions(SUBCSS_REGION(css_type)) for css_type in CSS_TYPES]
        if not any(css_regions):
            return

        # Find the region that is on the line that's being hovered next to; if
        # there is one, show the CSS navigation there. This determines the full
        # line that contains the marked CSS region and checks to see if the
        # hover point is within it.
        for idx, regions in enumerate(css_regions):
            if regions:
                if point in view.line(regions[0].begin()):
                    css_type = CSS_TYPES[idx]

                    content = f'<a href="{css_type}">Edit {css_type}</a>'
                    return view.show_popup(content, sublime.HIDE_ON_MOUSE_MOVE_AWAY,
                                           point, 1024, 512,
                                           lambda href: self.click(view, href))



## ----------------------------------------------------------------------------

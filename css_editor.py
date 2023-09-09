import sublime
import sublime_plugin

from os.path import basename, splitext
from bisect import bisect

import functools

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

# In the view from which the CSS is sourced to ull out into SubCSS tabs, this
# setting is applied to count the number of SubCSS tabs that are currently
# open as far as the SUBCSS_REGION keys are concerned; every time a region is
# added or removed, this value is updated.
CSS_SUBVIEW_COUNT="css_subview_count"


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


def log(message, *args, status=False, dialog=False):
    """
    Simple logging method; writes to the console and optionally also the status
    message as well.
    """
    message = message % args
    print("ColorSchemeCSSEditor:", message)
    if status:
        sublime.active_window().status_message(message)
    if dialog:
        sublime.message_dialog(message)


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


def get_sheet_group_for_view(view):
    """
    Given a view, get the sheet for that view and determine what group that
    sheet is currently contained in.
    """
    window = view.window()
    sheet = view.sheet()
    for group in range(window.num_groups()):
        if sheet in window.sheets_in_group(group):
            return group

    return None


def is_valid_css_region(view, regions):
    """
    Check to see if the provided list of region objects is a valid CSS region
    in the provided view.

    To be valid, the list must contain only a single region, that region needs
    to be of a minimum size and wrapped in double quotes.
    """
    if len(regions) != 1:
        return False

    r = regions[0]
    return (r.size() >= 2 and
                   all(view.substr(pt) == '"' for pt in (r.a, r.b - 1)))


def update_css_child_count(parent_view, delta):
    """
    Update the CSS_SUBVIEW_COUNT setting in the given parent view by the number
    provided in the delta. If this takes the count below 0,the value is clamped
    at zero.

    This will add the setting if it is not currently present and will remove
    the setting when its value would otherwise be zero.
    """
    current = parent_view.settings().get(CSS_SUBVIEW_COUNT, 0)
    current += delta

    if current <= 0:
        parent_view.settings().erase(CSS_SUBVIEW_COUNT)
    else:
        parent_view.settings().set(CSS_SUBVIEW_COUNT, current)


## ----------------------------------------------------------------------------


class ColorCommandBase():
    """
    This class is a holding area for helpful routines in commands that deal
    with examining and modifying color scheme files.
    """
    def find_key_region(self, key, key_regions):
        """
        Given the name of a JSON key and a list of regions that represent keys,
        return back the region that specifies the location of that key.

        The return value will be None if none of the regions in the given list
        is the desired key.
        """
        value = f'"{key}"'
        for region in key_regions:
            if self.view.substr(region) == value:
                return region

        return None


    def find_key_value_region(self, key_region, value_regions):
        """
        Given a region that represents a key in the file and a list of value
        regions, return the region from the list that associates with the given key
        region, such that the region provided is the value for the given key.

        The return value will be the found region.
        """
        v_points = [p.begin() for p in value_regions]
        return value_regions[bisect(v_points, key_region.a)]


    def get_global_region(self):
        """
        Given a view that represents a sublime-color-scheme file, return back a
        tuple of two regions, the first of which is the region that outlines where
        the "globals" key exists, and the second the region where the value for the
        "globals" key resides.

        The return value will be None if there is no globals key in the given color
        scheme view.
        """
        # Find the list of top level key regions and then find the one that
        # represents the globals key; if there is not one, we can return None right
        # away.
        keys = self.view.find_by_selector('meta.mapping.key - (meta.mapping.value meta.mapping.key)')
        global_key = self.find_key_region('globals', keys)
        if global_key is None:
            return None

        # Collect the regions that represent all of the top level values for keys
        # in the input file, find the one that aligns with the globals key.
        values = self.view.find_by_selector('meta.mapping.value')
        return (global_key, self.find_key_value_region(global_key, values))


    def get_global_key_values(self, key_list):
        """
        Given a view that represents a sublime-color-scheme file and a key or list
        of keys that should exist in the globals section of the color scheme, return
        a list of tuples that represents the regions that span the key name and the
        key value.

        A single key can be provided, or a list of keys. In either case the return
        value is a list, even if the list is empty. This includes when there is no
        globals key to actually contain the sub keys required.
        """
        if isinstance(key_list, str):
            key_list = [key_list]

        result = []

        # Find all of the regions that represent keys in the first nesting level
        # of the document.
        keys = self.view.find_by_selector('meta.mapping.value meta.mapping.key')
        values = self.view.find_by_selector('meta.mapping.value meta.mapping.value')

        # for each key in the key list, locate the region that spans that key, if
        # any.
        for key in key_list:
            key_region = self.find_key_region(key, keys)
            if key_region is not None:
                key_value = self.find_key_value_region(key_region, values)
                result.append((key_region, key_value))

        return sorted(result)


    def global_css_key(self, css_type):
        """
        Checks the current view, which is assumed to be a color scheme, to see
        if it has a global override key for the css type provided.

        The return value is a tuple that contains the region for the key and
        the region for the value of the key, or None if it that key does not
        exist.
        """
        if self.get_global_region() is not None:
            result = self.get_global_key_values(css_type)
            if result:
                return result[0]

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


## ----------------------------------------------------------------------------


class EditColorSchemeCssCommand(ColorCommandBase, sublime_plugin.TextCommand):
    """
    Given a color scheme CSS settings key which is one of the key outlined in
    CSS_TYPES, check the current view to see if it's an editable color scheme
    file that has that settings key in it.

    If it does, and there is not already an editor for that kind of CSS open
    already, this will open a scratch CSS view that contains the CSS content of
    the given key type to allow for editing.
    """
    def run(self, edit, css_type):
        # Get the region that spans the CSS type that the user wants to edit,
        # and the text within it.
        css_region = self.global_css_key(css_type)[1]
        css_data = self.view.substr(css_region)

        # Decode the extracted value to get the actual content; this will strip
        # away the double quotes since it is being treated as a JSON string.
        try:
            css_data = sublime.decode_value(css_data)
        except:
            return log(f'Unable to decode CSS for {css_type}', status=True)

        # Mark the region that we extracted with the CSS information.
        self.view.add_regions(SUBCSS_REGION(css_type), [css_region], **CSS_REGION_INFO)
        update_css_child_count(self.view, 1)

        # Check to see if there is already a view open for this particular type
        # of css edit; if there is and we are supposed to open it as a split,
        # then we might need to adjust the selected tabs.
        current_sub_view = find_css_view(self.view, css_type)
        if current_sub_view and cs_setting("open_as_split"):
            self.link_existing_css_view(current_sub_view)
        else:
            self.create_css_view(css_type, css_data)


    def link_existing_css_view(self, css_view):
        """
        Given an existing view that represents a CSS sub view which already
        exists, try to add that view to the list of selected tabs.

        This can only happen when both tabs are in the same file group in the
        same window and are both not already selected.

        If the current view is not in the tab selection, this will do nothing;
        this can really only happen when outside code triggers the command
        while the view is not active.
        """
        # Get the current group and see if the subview is also in it; if it is
        # not, we can't join it to the selection.
        group = get_sheet_group_for_view(self.view)
        if group and group != get_sheet_group_for_view(css_view):
            return

        # If we are in the list of selected sheets, add the sub view sheet to
        # the list if it is not already there.
        sheets = self.view.window().selected_sheets_in_group(group)
        if self.view.sheet() in sheets and css_view.sheet() not in sheets:
            sheets.append(css_view.sheet())
            self.view.window().select_sheets(sheets)


    def create_css_view(self, css_type, css_data):
        """
        Create a new SubCSS tab for the given CSS type which contains the
        provided CSS data to start with.

        The new tab will have the CSS syntax set, will have the flags that are
        used to convey information about the CSS type, and will be joined to
        the selection of tabs if the appropriate setting is set.
        """
        file_base_name = splitext(basename(self.view.file_name()))[0]

        # Create a new temporary CSS view in the current window; this
        # should have a distinguishing name, be set as a scratch buffer,
        # and have as content the extracted css_data.
        flags = sublime.ADD_TO_SELECTION if cs_setting("open_as_split") else 0
        temp = self.view.window().new_file(flags=flags , syntax=cs_setting("css_syntax"))
        temp.run_command("append", {"characters": css_data})
        temp.set_scratch(True)
        temp.set_name(f"Inline CSS: {file_base_name} {css_type}")

        # Set the subcss tab settings so that it knows that it's a sub-tab
        # and what source file spawned it.
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
        return self.global_css_key(css_type) is not None


    def is_visible(self, css_type):
        return self.is_enabled(css_type)


## ----------------------------------------------------------------------------


# Needs to work:
#   - when there is no globals section
#   - when the globals section is empty
#   - when the globals section has a css key in it
#   - when the globals section has no css key in it
#
# Extra credit:
#   Can we determine when a key exists but has no value? Do we even care?
#
# We will assume that the basic structure of the file is present
# - Inserting keys needs to make sure that the previous line has a comma or newline on the end, depending on what the line is=
#
class AddColorSchemeCssCommand(ColorCommandBase, sublime_plugin.TextCommand):
    """
    Given a color scheme CSS settings key which is one of the key outlined in
    CSS_TYPES, check the current view to see if it's an editable color scheme
    file that has that settings key in it.

    If that key is not present, this will add the key to the file with a
    default value and then trigger the command that edits the key in place.
    """
    def run(self, edit, css_type):
        # The key value that we want to insert into the globals section for
        # this css type.
        new_key = f'"{css_type}": "",'

        # If there is not currently a globals section in the file, then we
        # should add one.
        if self.get_global_region() is None:
            # Find the list of section starts; the position of the first one
            # is where we want to do our insertion. If there are none, we
            # cannot proceed because the file is empty.
            start = self.view.find_by_selector('punctuation.section.mapping.begin.json')
            if not start:
                return log(f'unable to determine where to insert the new {css_type} key', status=True)

            # Set the insertion point for the stub to be the first character
            # after the start of the first section of the document, and set
            # the text to insert to be an entire globals section which has the
            # new key in it.
            insert_pt = start[0].a + 1
            insert_text = f'\n\t"globals":\n\t{{\n\t\t{new_key}\n\t}},'

        else:
            # Get the list of existing CSS keys, if any
            regions = self.get_global_key_values(CSS_TYPES)

            # If we found some, then insert after the end of the last value
            # found. The list is an array of tuples where the second item in
            # the tuple is the region for the value.
            if regions:
                insert_pt = regions[-1][1].b + 1
                insert_text = f'\n\t\t{new_key}'

            else:
                # When there are no CSS keys, insert right at the end of the
                # globals section.
                global_region = self.get_global_region()[1]
                insert_pt = global_region.b - 1
                insert_text = f'\t{new_key}\n\t'

        # Insert the text now, and then trigger the edit
        self.view.insert(edit, insert_pt, insert_text)
        self.view.run_command('edit_color_scheme_css', {'css_type': css_type})


    def is_enabled(self, css_type):
        # If the css type given is not valid, or this is not a color scheme
        # file the user can edit, we can't be enabled.
        if css_type not in CSS_TYPES or not self.is_editable_color_scheme():
            return False

        # If this color scheme file does not have the appropriate key, then we
        # can do the addition.
        return self.global_css_key(css_type) is None


    def is_visible(self, css_type):
        return self.is_enabled(css_type)


## ----------------------------------------------------------------------------


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


## ----------------------------------------------------------------------------


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
            return log('unable to find the source file to save the CSS to', status=True)

        # If white space trimming on save is turned on, then trim our content
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


## ----------------------------------------------------------------------------


class DoSubCssReplaceCommand(sublime_plugin.TextCommand):
    """
    This command accepts a string of CSS data, which it will attempt to insert
    into the view within the subcss marked region; if there is no such region
    this will do nothing.

    This command is intended for internal package use only.
    """
    def run(self, edit, css, css_type):
        # Find the region into which the CSS should be inserted; this should be
        # a single region which is valid to contain CSS. If that is not the
        # case, then fail.
        source = self.view.get_regions(SUBCSS_REGION(css_type))
        if not source or not is_valid_css_region(self.view, source):
            if len(source) != 1:
                return log(f'unable to find the CSS source region for {css_type} to replace', status=True)

            self.view.erase_regions(SUBCSS_REGION(css_type))
            update_css_child_count(self.view, -1)

            return log(f"the value for {css_type} in the source file is missing or invalid", status=True)

        # The region we want to insert into is the first (and only) region that
        # we found, and we can proceed with the insertion; calculate the
        # difference in size between the content we will insert and the size of
        # the region as it currently exists.
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
            update_css_child_count(self.view, 0)


## ----------------------------------------------------------------------------


class OverallCssListener(sublime_plugin.EventListener):
    """
    This listens for views that are about to close; if the view being closed is
    the source of a CSS sub-tab, then the CSS sub-tab will also be closed.

    Also, if the view being closed is a CSS sub-tab, then the handler will
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
        # a sub-tab, then find the sub-tab and close it.
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
                update_css_child_count(main_view, -1)


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


class CSSRemoveDeletedRegionsEventListener(sublime_plugin.ViewEventListener):
    """
    Listen for modifications to color scheme views that currently have one or
    more subcss views open. When a modification removes (i.e. makes empty) a
    subcss region, cull it from the view so that the gutter icon gets removed
    and attempts to update the CSS do not work.
    """
    pending = 0

    @classmethod
    def is_applicable(cls, settings):
        return settings.has(CSS_SUBVIEW_COUNT)


    def on_modified_async(self):
        self.pending += 1
        sublime.set_timeout_async(functools.partial(self.check_regions), 1000)


    def check_regions(self):
        self.pending -= 1
        if self.pending != 0:
            return

        # Scan over all CSS regions; we find a region list but that list is
        # not valid, then prune that away from the view.
        for css_type in CSS_TYPES:
            regions = self.view.get_regions(SUBCSS_REGION(css_type))
            if regions and not is_valid_css_region(self.view, regions):
                self.view.erase_regions(SUBCSS_REGION(css_type))
                update_css_child_count(self.view, -1)


## ----------------------------------------------------------------------------

"""Pure helpers for persistent Treeview column visibility."""


DEFAULT_VISIBLE_COLUMNS = frozenset({
    "feature",
    "status",
    "clickup_status",
    "frequency",
    "last_update",
    "next_due",
    "last_duration",
    "last_status_session",
    "sqlite_compare",
    "attachment_status",
    "task_sync",
})


def default_column_visibility(columns):
    return {
        name: name in DEFAULT_VISIBLE_COLUMNS
        for name in columns
    }

def normalize_column_visibility(columns, stored=None):
    state = default_column_visibility(columns)
    if isinstance(stored, dict):
        for name in state:
            value = stored.get(name)
            if isinstance(value, bool):
                state[name] = value
    return state


def visible_columns(columns, stored=None):
    state = normalize_column_visibility(columns, stored)
    return tuple(name for name in columns if state[name])

def displayed_columns(columns, displaycolumns):
    """
    Resolve a Treeview displaycolumns value into logical column names.

    Treeview's #1, #2, ... identifiers refer to displayed positions, not
    necessarily to the original order in the columns option. Hidden or
    reordered columns therefore change what each displayed position means.
    """
    columns = tuple(str(name) for name in columns)

    if displaycolumns == "#all":
        return columns

    if isinstance(displaycolumns, str):
        values = (displaycolumns,)
    else:
        values = tuple(displaycolumns)

    if values == ("#all",):
        return columns

    resolved = []

    for value in values:
        text = str(value)

        if text in columns:
            resolved.append(text)
            continue

        try:
            index = int(text)
        except (TypeError, ValueError):
            continue

        if 0 <= index < len(columns):
            resolved.append(columns[index])

    return tuple(resolved)


def display_column_name(columns, displaycolumns, column_id):
    """
    Resolve a displayed Treeview identifier such as #2 to its logical name.
    """
    column_id = str(column_id or "")

    if column_id == "#0":
        return "#0"

    if not column_id.startswith("#"):
        return None

    try:
        display_index = int(column_id[1:]) - 1
    except ValueError:
        return None

    visible = displayed_columns(
        columns,
        displaycolumns,
    )

    if display_index < 0 or display_index >= len(visible):
        return None

    return visible[display_index]


def display_column_id(columns, displaycolumns, column_name):
    """
    Resolve a logical column name to its current displayed #n identifier.

    Returns None when that logical column is currently hidden.
    """
    visible = displayed_columns(
        columns,
        displaycolumns,
    )

    try:
        display_index = visible.index(str(column_name))
    except ValueError:
        return None

    return f"#{display_index + 1}"
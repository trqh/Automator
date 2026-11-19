import json

from automator.ui.gui_columns import (
    default_column_visibility,
    display_column_id,
    display_column_name,
    displayed_columns,
    normalize_column_visibility,
    visible_columns,
)

def test_default_column_visibility_matches_canonical_main_view():
    columns = (
        "feature",
        "status",
        "clickup_status",
        "frequency",
        "last_update",
        "next_due",
        "notes",
        "bugs",
        "last_duration",
        "last_status_saved",
        "last_status_session",
        "run_duration_session",
        "sqlite_copied_session",
        "timeout_sec",
        "work_timer",
        "setup_state",
        "sqlite_compare",
        "person_count",
        "output_class",
        "clickup_task",
        "attachment_status",
        "task_sync",
        "time_sync",
    )

    assert visible_columns(
        columns
    ) == (
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
    )


def test_column_visibility_round_trips_and_adds_future_columns_safely(tmp_path):
    old_columns = (
        "feature",
        "notes",
        "sqlite_compare",
    )

    state = default_column_visibility(
        old_columns
    )

    # Persist deliberate user overrides in both directions.
    state["feature"] = False
    state["notes"] = True

    path = tmp_path / "gui_state.json"

    path.write_text(
        json.dumps(
            {
                "column_visibility": state,
            }
        ),
        encoding="utf-8",
    )

    loaded = json.loads(
        path.read_text(
            encoding="utf-8"
        )
    )["column_visibility"]

    new_columns = old_columns + (
        "future_column",
    )

    normalized = normalize_column_visibility(
        new_columns,
        loaded,
    )

    assert normalized["feature"] is False
    assert normalized["notes"] is True
    assert normalized["sqlite_compare"] is True

    # Newly introduced columns stay hidden until deliberately
    # added to the canonical default.
    assert normalized["future_column"] is False

    assert visible_columns(
        new_columns,
        normalized,
    ) == (
        "notes",
        "sqlite_compare",
    )

def test_invalid_visibility_values_use_defaults():
    assert normalize_column_visibility(
        ("feature", "notes"),
        {"feature": "false"},
    ) == {
        "feature": True,
        "notes": False,
    }

def test_displayed_columns_resolve_hidden_and_reordered_columns():
    columns = (
        "feature",
        "status",
        "frequency",
        "notes",
        "bugs",
    )

    assert displayed_columns(
        columns,
        ("feature", "frequency", "notes"),
    ) == (
        "feature",
        "frequency",
        "notes",
    )

    assert displayed_columns(
        columns,
        ("notes", "feature", "status"),
    ) == (
        "notes",
        "feature",
        "status",
    )


def test_displayed_columns_support_treeview_all_columns_marker():
    columns = (
        "feature",
        "status",
        "notes",
    )

    assert displayed_columns(
        columns,
        "#all",
    ) == columns

    assert displayed_columns(
        columns,
        ("#all",),
    ) == columns


def test_display_column_name_uses_current_display_order():
    columns = (
        "feature",
        "status",
        "frequency",
        "notes",
        "bugs",
    )
    displaycolumns = (
        "feature",
        "notes",
        "bugs",
    )

    assert display_column_name(
        columns,
        displaycolumns,
        "#1",
    ) == "feature"

    assert display_column_name(
        columns,
        displaycolumns,
        "#2",
    ) == "notes"

    assert display_column_name(
        columns,
        displaycolumns,
        "#3",
    ) == "bugs"

    assert display_column_name(
        columns,
        displaycolumns,
        "#4",
    ) is None


def test_display_column_id_follows_column_when_visibility_changes():
    columns = (
        "feature",
        "status",
        "frequency",
        "notes",
        "bugs",
    )

    assert display_column_id(
        columns,
        columns,
        "notes",
    ) == "#4"

    assert display_column_id(
        columns,
        ("feature", "notes", "bugs"),
        "notes",
    ) == "#2"

    assert display_column_id(
        columns,
        ("feature", "status", "bugs"),
        "notes",
    ) is None


def test_display_column_helpers_support_reordered_columns():
    columns = (
        "feature",
        "status",
        "notes",
    )
    displaycolumns = (
        "notes",
        "feature",
        "status",
    )

    assert display_column_name(
        columns,
        displaycolumns,
        "#1",
    ) == "notes"

    assert display_column_id(
        columns,
        displaycolumns,
        "feature",
    ) == "#2"
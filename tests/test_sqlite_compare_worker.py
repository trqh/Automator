import sqlite3
from datetime import date
import pytest
import automator.workers.sqlite_compare_worker as sqlite_worker
from automator.workers.sqlite_compare_worker import (
    ComparisonError,
    compare_feature,
    inspect_sqlite_differences,
    safe_replace_from_candidate,
    summarize_sqlite_comparison,
)


def setup_paths(tmp_path, feature="41"):
    runs = tmp_path / "runs"
    candidate_dir = runs / ("feature_" + feature)
    canonical_dir = tmp_path / "Canonical Outputs"
    candidate_dir.mkdir(parents=True)
    canonical_dir.mkdir()
    return runs, candidate_dir / (feature + ".sqlite"), canonical_dir, canonical_dir / (feature + ".sqlite")


def test_reports_missing_candidate_and_canonical(tmp_path):
    runs, candidate, canonical_dir, canonical = setup_paths(tmp_path)
    assert compare_feature("41", runs, canonical_dir).state == "NO CANDIDATE"
    candidate.write_bytes(b"candidate")
    assert compare_feature("41", runs, canonical_dir).state == "NO CANONICAL"

def test_different_does_not_overwrite_canonical_until_accept(
    tmp_path,
    monkeypatch,
):
    (
        runs,
        candidate,
        canonical_dir,
        canonical,
    ) = setup_paths(
        tmp_path
    )

    create_database(
        canonical,
        [
            (
                "CREATE TABLE item "
                "(id INTEGER PRIMARY KEY, value TEXT)",
                (),
            ),
            (
                "INSERT INTO item VALUES (?, ?)",
                (
                    1,
                    "old",
                ),
            ),
        ],
    )

    create_database(
        candidate,
        [
            (
                "CREATE TABLE item "
                "(id INTEGER PRIMARY KEY, value TEXT)",
                (),
            ),
            (
                "INSERT INTO item VALUES (?, ?)",
                (
                    1,
                    "new",
                ),
            ),
        ],
    )

    assert (
        compare_feature(
            "41",
            runs,
            canonical_dir,
        ).state
        == "DIFFERENT"
    )

    original = (
        canonical.read_bytes()
    )

    def simulated_atomic_replace(
        source,
        destination,
    ):
        from pathlib import Path

        Path(destination).write_bytes(
            Path(source).read_bytes()
        )

        Path(source).unlink()

    monkeypatch.setattr(
        "automator.workers.sqlite_compare_worker.os.replace",
        simulated_atomic_replace,
    )

    safe_replace_from_candidate(
        candidate,
        canonical,
    )

    assert (
        canonical.read_bytes()
        == candidate.read_bytes()
    )

    assert (
        canonical.read_bytes()
        != original
    )


def test_same_comparison_does_not_replace_canonical(
    tmp_path,
    monkeypatch,
):
    (
        runs,
        candidate,
        canonical_dir,
        canonical,
    ) = setup_paths(
        tmp_path
    )

    statements = [
        (
            "CREATE TABLE item "
            "(id INTEGER PRIMARY KEY, value TEXT)",
            (),
        ),
        (
            "INSERT INTO item VALUES (?, ?)",
            (
                1,
                "same",
            ),
        ),
    ]

    create_database(
        candidate,
        statements,
    )

    create_database(
        canonical,
        statements,
    )

    replace_calls = []

    def simulated_atomic_replace(
        source,
        destination,
    ):
        replace_calls.append(
            (
                source,
                destination,
            )
        )

    monkeypatch.setattr(
        "automator.workers.sqlite_compare_worker.os.replace",
        simulated_atomic_replace,
    )

    result = compare_feature(
        "41",
        runs,
        canonical_dir,
    )

    assert (
        result.state
        == "SAME"
    )

    assert (
        replace_calls
        == []
    )

def test_safe_replace_preserves_existing_canonical_on_missing_candidate(tmp_path):
    canonical = tmp_path / "canonical.sqlite"
    canonical.write_bytes(b"keep")
    with pytest.raises(ComparisonError):
        safe_replace_from_candidate(tmp_path / "missing.sqlite", canonical)
    assert canonical.read_bytes() == b"keep"

def test_safe_replace_archives_existing_canonical_before_replacement(
    tmp_path,
):
    canonical_dir = tmp_path / "Canonical Outputs"
    canonical_dir.mkdir()

    candidate = tmp_path / "candidate.sqlite"
    canonical = canonical_dir / "49822.sqlite"

    candidate.write_bytes(b"new canonical")
    canonical.write_bytes(b"old canonical")

    safe_replace_from_candidate(
        candidate,
        canonical,
    )

    archive = (
        tmp_path
        / "Canonical Output History"
        / "49822"
        / (
            "49822_"
            + date.today().isoformat()
            + ".sqlite"
        )
    )

    assert archive.is_file()
    assert archive.read_bytes() == b"old canonical"
    assert canonical.read_bytes() == b"new canonical"


def test_safe_replace_keeps_first_history_snapshot_on_same_day(
    tmp_path,
):
    canonical_dir = tmp_path / "Canonical Outputs"
    canonical_dir.mkdir()

    candidate = tmp_path / "candidate.sqlite"
    canonical = canonical_dir / "49822.sqlite"

    canonical.write_bytes(b"first canonical")
    candidate.write_bytes(b"second canonical")

    safe_replace_from_candidate(
        candidate,
        canonical,
    )

    candidate.write_bytes(b"third canonical")

    safe_replace_from_candidate(
        candidate,
        canonical,
    )

    history_dir = (
        tmp_path
        / "Canonical Output History"
        / "49822"
    )

    archive = (
        history_dir
        / (
            "49822_"
            + date.today().isoformat()
            + ".sqlite"
        )
    )

    snapshots = tuple(history_dir.glob("*.sqlite"))
    assert len(snapshots) == 2
    assert archive in snapshots
    assert archive.read_bytes() == b"first canonical"
    assert {p.read_bytes() for p in snapshots} == {
        b"first canonical", b"second canonical"
    }
    assert canonical.read_bytes() == b"third canonical"


def test_safe_replace_without_existing_canonical_creates_no_history(
    tmp_path,
):
    canonical_dir = tmp_path / "Canonical Outputs"
    canonical_dir.mkdir()

    candidate = tmp_path / "candidate.sqlite"
    canonical = canonical_dir / "49822.sqlite"

    candidate.write_bytes(b"first canonical")

    safe_replace_from_candidate(
        candidate,
        canonical,
    )

    assert canonical.read_bytes() == b"first canonical"
    assert not (
        tmp_path
        / "Canonical Output History"
    ).exists()


def test_safe_replace_archive_failure_preserves_existing_canonical(
    tmp_path,
    monkeypatch,
):
    canonical_dir = tmp_path / "Canonical Outputs"
    canonical_dir.mkdir()

    candidate = tmp_path / "candidate.sqlite"
    canonical = canonical_dir / "49822.sqlite"

    candidate.write_bytes(b"new canonical")
    canonical.write_bytes(b"old canonical")

    def fail_history_link(
        source,
        destination,
    ):
        raise OSError(
            "simulated history failure"
        )

    monkeypatch.setattr(
        sqlite_worker.os,
        "link",
        fail_history_link,
    )

    with pytest.raises(
        ComparisonError,
        match="Could not archive canonical file",
    ):
        safe_replace_from_candidate(
            candidate,
            canonical,
        )

    assert canonical.read_bytes() == b"old canonical"
    assert candidate.read_bytes() == b"new canonical"

    history_dir = (
        tmp_path
        / "Canonical Output History"
        / "49822"
    )

    assert not list(
        history_dir.glob("*.sqlite")
    )

    assert not list(
        history_dir.glob("*.tmp")
    )

def create_database(path, statements):
    connection = sqlite3.connect(path)
    try:
        for statement, values in statements:
            connection.execute(statement, values)
        connection.commit()
    finally:
        connection.close()


def test_inspection_reports_changed_columns_for_primary_key_row(tmp_path):
    candidate = tmp_path / "candidate.sqlite"
    canonical = tmp_path / "canonical.sqlite"
    schema = [("CREATE TABLE specification (id INTEGER PRIMARY KEY, import_code TEXT, list_update TEXT)", ())]
    create_database(canonical, schema + [("INSERT INTO specification VALUES (?, ?, ?)", (1, "old", "2026-10-12"))])
    create_database(candidate, schema + [("INSERT INTO specification VALUES (?, ?, ?)", (1, "new", "2026-10-13"))])

    report = inspect_sqlite_differences(candidate, canonical)

    difference = report.table_differences[0]
    assert difference.state == "DATA DIFFERENT"
    assert difference.message == "1 changed"
    assert difference.examples[0]["key"] == {"id": "1"}
    assert difference.examples[0]["columns"] == {
        "import_code": {"candidate": "new", "canonical": "old"},
        "list_update": {"candidate": "2026-10-13", "canonical": "2026-10-12"},
    }


def test_inspection_reports_inserted_and_deleted_rows(tmp_path):
    candidate = tmp_path / "candidate.sqlite"
    canonical = tmp_path / "canonical.sqlite"
    schema = [("CREATE TABLE item (part TEXT, code INTEGER, value TEXT, PRIMARY KEY (part, code))", ())]
    create_database(canonical, schema + [
        ("INSERT INTO item VALUES (?, ?, ?)", ("removed", 1, "old")),
        ("INSERT INTO item VALUES (?, ?, ?)", ("same", 2, "same")),
    ])
    create_database(candidate, schema + [
        ("INSERT INTO item VALUES (?, ?, ?)", ("added", 3, "new")),
        ("INSERT INTO item VALUES (?, ?, ?)", ("same", 2, "same")),
    ])

    difference = inspect_sqlite_differences(candidate, canonical).table_differences[0]

    assert difference.state == "DATA DIFFERENT"
    assert difference.message == "1 added, 1 removed"
    assert {example["kind"] for example in difference.examples} == {"added", "removed"}


def test_inspection_uses_binary_order_for_keyed_text_with_declared_collation(tmp_path):
    candidate = tmp_path / "candidate.sqlite"
    canonical = tmp_path / "canonical.sqlite"
    schema = [("CREATE TABLE item (code TEXT COLLATE NOCASE PRIMARY KEY, value TEXT)", ())]
    create_database(canonical, schema + [
        ("INSERT INTO item VALUES (?, ?)", ("A", "canonical A")),
        ("INSERT INTO item VALUES (?, ?)", ("b", "canonical b")),
    ])
    create_database(candidate, schema + [
        ("INSERT INTO item VALUES (?, ?)", ("B", "candidate B")),
        ("INSERT INTO item VALUES (?, ?)", ("a", "candidate a")),
    ])

    difference = inspect_sqlite_differences(candidate, canonical).table_differences[0]

    assert difference.state == "DATA DIFFERENT"
    assert difference.message == "2 added, 2 removed"
    assert [example["kind"] for example in difference.examples] == ["removed", "added", "added", "removed"]


def test_inspection_reports_missing_table_and_schema_difference(tmp_path):
    candidate = tmp_path / "candidate.sqlite"
    canonical = tmp_path / "canonical.sqlite"
    create_database(canonical, [
        ("CREATE TABLE shared (id INTEGER PRIMARY KEY, value TEXT)", ()),
        ("CREATE TABLE removed_table (id INTEGER PRIMARY KEY)", ()),
    ])
    create_database(candidate, [
        ("CREATE TABLE shared (id INTEGER PRIMARY KEY, value TEXT, revision INTEGER)", ()),
        ("CREATE TABLE added_table (id INTEGER PRIMARY KEY)", ()),
    ])

    differences = {item.table_name: item for item in inspect_sqlite_differences(candidate, canonical).table_differences}

    assert differences["added_table"].state == "TABLE ADDED"
    assert differences["removed_table"].state == "TABLE REMOVED"
    assert differences["shared"].state == "SCHEMA DIFFERENT"


def test_inspection_reports_same_count_change_without_primary_key(tmp_path):
    candidate = tmp_path / "candidate.sqlite"
    canonical = tmp_path / "canonical.sqlite"
    create_database(canonical, [
        ("CREATE TABLE event (description TEXT)", ()),
        ("INSERT INTO event VALUES (?)", ("old",)),
    ])
    create_database(candidate, [
        ("CREATE TABLE event (description TEXT)", ()),
        ("INSERT INTO event VALUES (?)", ("new",)),
    ])

    difference = inspect_sqlite_differences(candidate, canonical).table_differences[0]

    assert difference.state == "DATA DIFFERENT"
    assert difference.message == "1 added, 1 removed; no primary key"
    assert {example["kind"] for example in difference.examples} == {"added", "removed"}
    assert {example["candidate"]["description"] for example in difference.examples if "candidate" in example} == {"new"}
    assert {example["canonical"]["description"] for example in difference.examples if "canonical" in example} == {"old"}


def test_inspection_compares_no_primary_key_rows_as_multisets(tmp_path):
    candidate = tmp_path / "candidate.sqlite"
    canonical = tmp_path / "canonical.sqlite"
    create_database(canonical, [
        ("CREATE TABLE event (description TEXT)", ()),
        ("INSERT INTO event VALUES (?)", ("same",)),
        ("INSERT INTO event VALUES (?)", ("removed",)),
    ])
    create_database(candidate, [
        ("CREATE TABLE event (description TEXT)", ()),
        ("INSERT INTO event VALUES (?)", ("same",)),
        ("INSERT INTO event VALUES (?)", ("added",)),
    ])

    difference = inspect_sqlite_differences(candidate, canonical).table_differences[0]

    assert difference.state == "DATA DIFFERENT"
    assert difference.message == "1 added, 1 removed; no primary key"
    assert {example["kind"] for example in difference.examples} == {"added", "removed"}

def create_person_identity_database(
    path,
    *,
    reordered=False,
    rationale_details="same details",
):
    connection = sqlite3.connect(path)

    try:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE person (
                id INTEGER PRIMARY KEY,
                full_name TEXT
            );

            CREATE TABLE identifiers (
                id INTEGER PRIMARY KEY,
                value TEXT,
                person_id INTEGER,
                FOREIGN KEY (person_id)
                    REFERENCES person(id)
            );

            CREATE TABLE code (
                id INTEGER PRIMARY KEY,
                person_id INTEGER,
                code TEXT,
                FOREIGN KEY (person_id)
                    REFERENCES person(id)
            );

            CREATE TABLE person_specification (
                id INTEGER PRIMARY KEY,
                person_id INTEGER,
                specification_id INTEGER,
                FOREIGN KEY (person_id)
                    REFERENCES person(id)
            );

            CREATE TABLE rationale (
                id INTEGER PRIMARY KEY,
                details TEXT,
                person_spec_id INTEGER,
                rationale_type_id INTEGER,
                lent_id INTEGER,
                rationale_id INTEGER,
                date_start TEXT,
                date_end TEXT,
                function_type_id INTEGER,
                function_type_name TEXT,
                FOREIGN KEY (person_spec_id)
                    REFERENCES person_specification(id),
                FOREIGN KEY (rationale_id)
                    REFERENCES rationale(id)
            );

            CREATE TABLE link (
                id INTEGER PRIMARY KEY,
                rationale_id INTEGER,
                link TEXT,
                FOREIGN KEY (rationale_id)
                    REFERENCES rationale(id)
            );
            """
        )

        if reordered:
            alice_person_id = 7
            bob_person_id = 1

            alice_person_spec_id = 20
            bob_person_spec_id = 10

            alice_rationale_id = 200
            bob_rationale_id = 100

            alice_link_id = 2000
            bob_link_id = 1000

        else:
            alice_person_id = 1
            bob_person_id = 7

            alice_person_spec_id = 10
            bob_person_spec_id = 20

            alice_rationale_id = 100
            bob_rationale_id = 200

            alice_link_id = 1000
            bob_link_id = 2000

        people = [
            (
                alice_person_id,
                "Alice Example",
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "A-1",
                alice_person_spec_id,
                alice_rationale_id,
                alice_link_id,
                rationale_details,
            ),
            (
                bob_person_id,
                "Bob Example",
                "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "B-1",
                bob_person_spec_id,
                bob_rationale_id,
                bob_link_id,
                "bob details",
            ),
        ]

        for (
            person_id,
            name,
            identifier,
            code,
            person_spec_id,
            rationale_id,
            link_id,
            details,
        ) in people:
            connection.execute(
                "INSERT INTO person VALUES (?, ?)",
                (
                    person_id,
                    name,
                ),
            )

            connection.execute(
                "INSERT INTO identifiers VALUES (?, ?, ?)",
                (
                    person_id,
                    identifier,
                    person_id,
                ),
            )

            connection.execute(
                "INSERT INTO code VALUES (?, ?, ?)",
                (
                    person_id,
                    person_id,
                    code,
                ),
            )

            connection.execute(
                "INSERT INTO person_specification "
                "VALUES (?, ?, ?)",
                (
                    person_spec_id,
                    person_id,
                    1,
                ),
            )

            connection.execute(
                "INSERT INTO rationale "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rationale_id,
                    details,
                    person_spec_id,
                    1,
                    None,
                    None,
                    "2026-10-20",
                    None,
                    None,
                    None,
                ),
            )

            connection.execute(
                "INSERT INTO link VALUES (?, ?, ?)",
                (
                    link_id,
                    rationale_id,
                    (
                        "https://example.test/"
                        + identifier
                    ),
                ),
            )

        connection.commit()

    finally:
        connection.close()


def test_person_identifier_normalization_ignores_generated_id_reordering(
    tmp_path,
):
    (
        runs,
        candidate,
        canonical_dir,
        canonical,
    ) = setup_paths(
        tmp_path
    )

    create_person_identity_database(
        canonical,
        reordered=False,
    )

    create_person_identity_database(
        candidate,
        reordered=True,
    )

    result = compare_feature(
        "41",
        runs,
        canonical_dir,
    )

    assert (
        result.state
        == "SAME"
    )

    assert (
        result.byte_identical
        is False
    )

    differences = {
        item.table_name: item
        for item in (
            inspect_sqlite_differences(
                candidate,
                canonical,
            ).table_differences
        )
    }

    assert all(
        item.state == "SAME"
        for item
        in differences.values()
    )


def test_person_identifier_normalization_still_detects_real_related_data_change(
    tmp_path,
):
    (
        runs,
        candidate,
        canonical_dir,
        canonical,
    ) = setup_paths(
        tmp_path
    )

    create_person_identity_database(
        canonical,
        reordered=False,
        rationale_details="old details",
    )

    create_person_identity_database(
        candidate,
        reordered=True,
        rationale_details="new details",
    )

    result = compare_feature(
        "41",
        runs,
        canonical_dir,
    )

    assert (
        result.state
        == "DIFFERENT"
    )

    differences = {
        item.table_name: item
        for item in (
            inspect_sqlite_differences(
                candidate,
                canonical,
            ).table_differences
        )
    }

    assert (
        differences["person"].state
        == "SAME"
    )

    assert (
        differences["identifiers"].state
        == "SAME"
    )

    assert (
        differences["code"].state
        == "SAME"
    )

    assert (
        differences["link"].state
        == "SAME"
    )

    assert (
        differences["rationale"].state
        == "DATA DIFFERENT"
    )

def add_third_person(path):
    connection = sqlite3.connect(path)

    try:
        connection.execute(
            "INSERT INTO person VALUES (?, ?)",
            (
                30,
                "Charlie Example",
            ),
        )

        connection.execute(
            "INSERT INTO identifiers VALUES (?, ?, ?)",
            (
                30,
                "cccccccccccccccccccccccccccccccc",
                30,
            ),
        )

        connection.execute(
            "INSERT INTO code VALUES (?, ?, ?)",
            (
                30,
                30,
                "C-1",
            ),
        )

        connection.execute(
            "INSERT INTO person_specification VALUES (?, ?, ?)",
            (
                300,
                30,
                1,
            ),
        )

        connection.execute(
            "INSERT INTO rationale "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                3000,
                "charlie details",
                300,
                1,
                None,
                None,
                "2026-10-20",
                None,
                None,
                None,
            ),
        )

        connection.execute(
            "INSERT INTO link VALUES (?, ?, ?)",
            (
                30000,
                3000,
                "https://example.test/"
                "cccccccccccccccccccccccccccccccc",
            ),
        )

        connection.commit()

    finally:
        connection.close()


def test_summary_classifies_same_after_identifier_normalization(
    tmp_path,
):
    (
        _runs,
        candidate,
        _canonical_dir,
        canonical,
    ) = setup_paths(
        tmp_path
    )

    create_person_identity_database(
        canonical,
        reordered=False,
    )

    create_person_identity_database(
        candidate,
        reordered=True,
    )

    summary = summarize_sqlite_comparison(
        candidate,
        canonical,
    )

    assert summary.tier == "SAME"

    assert (
        summary.candidate_entity_count
        == 2
    )

    assert (
        summary.canonical_entity_count
        == 2
    )

    assert (
        summary.matched_entity_count
        == 2
    )

    assert summary.added_identifiers == ()
    assert summary.removed_identifiers == ()

    assert (
        summary.changed_entity_count
        == 0
    )

    assert summary.changed_identifiers == ()
    assert summary.changed_tables == ()
    assert summary.schema_changes == ()


def test_summary_classifies_data_change_on_matched_entity(
    tmp_path,
):
    (
        _runs,
        candidate,
        _canonical_dir,
        canonical,
    ) = setup_paths(
        tmp_path
    )

    create_person_identity_database(
        canonical,
        reordered=False,
        rationale_details="old details",
    )

    create_person_identity_database(
        candidate,
        reordered=True,
        rationale_details="new details",
    )

    summary = summarize_sqlite_comparison(
        candidate,
        canonical,
    )

    assert (
        summary.tier
        == "DATA CHANGED"
    )

    assert (
        summary.matched_entity_count
        == 2
    )

    assert (
        summary.changed_entity_count
        == 1
    )

    assert summary.changed_identifiers == (
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )

    assert summary.changed_tables == (
        "rationale",
    )


def test_summary_classifies_entity_population_change(
    tmp_path,
):
    (
        _runs,
        candidate,
        _canonical_dir,
        canonical,
    ) = setup_paths(
        tmp_path
    )

    create_person_identity_database(
        canonical,
        reordered=False,
    )

    create_person_identity_database(
        candidate,
        reordered=True,
    )

    add_third_person(
        candidate
    )

    summary = summarize_sqlite_comparison(
        candidate,
        canonical,
    )

    assert (
        summary.tier
        == "ENTITIES CHANGED"
    )

    assert (
        summary.candidate_entity_count
        == 3
    )

    assert (
        summary.canonical_entity_count
        == 2
    )

    assert (
        summary.matched_entity_count
        == 2
    )

    assert summary.added_identifiers == (
        "cccccccccccccccccccccccccccccccc",
    )

    assert summary.removed_identifiers == ()

    assert (
        summary.changed_entity_count
        == 0
    )


def test_summary_classifies_entities_plus_data_change(
    tmp_path,
):
    (
        _runs,
        candidate,
        _canonical_dir,
        canonical,
    ) = setup_paths(
        tmp_path
    )

    create_person_identity_database(
        canonical,
        reordered=False,
        rationale_details="old details",
    )

    create_person_identity_database(
        candidate,
        reordered=True,
        rationale_details="new details",
    )

    add_third_person(
        candidate
    )

    summary = summarize_sqlite_comparison(
        candidate,
        canonical,
    )

    assert (
        summary.tier
        == "ENTITIES + DATA CHANGED"
    )

    assert summary.added_identifiers == (
        "cccccccccccccccccccccccccccccccc",
    )

    assert (
        summary.changed_entity_count
        == 1
    )

    assert summary.changed_identifiers == (
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )


def test_summary_classifies_structure_change(
    tmp_path,
):
    candidate = (
        tmp_path
        / "candidate.sqlite"
    )

    canonical = (
        tmp_path
        / "canonical.sqlite"
    )

    create_database(
        canonical,
        [
            (
                "CREATE TABLE item "
                "(id INTEGER PRIMARY KEY, value TEXT)",
                (),
            ),
        ],
    )

    create_database(
        candidate,
        [
            (
                "CREATE TABLE item "
                "(id INTEGER PRIMARY KEY, value TEXT, revision INTEGER)",
                (),
            ),
        ],
    )

    summary = summarize_sqlite_comparison(
        candidate,
        canonical,
    )

    assert (
        summary.tier
        == "STRUCTURE CHANGED"
    )

    assert summary.schema_changes == (
        "table:item",
    )


def test_summary_ignores_documented_volatile_specification_values(
    tmp_path,
):
    candidate = (
        tmp_path
        / "candidate.sqlite"
    )

    canonical = (
        tmp_path
        / "canonical.sqlite"
    )

    schema = [
        (
            "CREATE TABLE specification "
            "(id INTEGER PRIMARY KEY, import_code TEXT, list_update TEXT)",
            (),
        ),
    ]

    create_database(
        canonical,
        schema + [
            (
                "INSERT INTO specification VALUES (?, ?, ?)",
                (
                    1,
                    "ABC-2026.10.20",
                    "2026-10-20",
                ),
            ),
        ],
    )

    create_database(
        candidate,
        schema + [
            (
                "INSERT INTO specification VALUES (?, ?, ?)",
                (
                    1,
                    "ABC-2026.10.21",
                    "2026-10-21",
                ),
            ),
        ],
    )

    summary = summarize_sqlite_comparison(
        candidate,
        canonical,
    )

    assert summary.tier == "SAME"
    assert summary.changed_tables == ()

def create_duplicate_rationale_database(
    path,
    *,
    reordered=False,
    second_details="Ground B",
    swap_addresses=False,
):
    connection = sqlite3.connect(path)

    try:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE person (
                id INTEGER PRIMARY KEY,
                full_name TEXT
            );

            CREATE TABLE identifiers (
                id INTEGER PRIMARY KEY,
                value TEXT,
                person_id INTEGER,
                FOREIGN KEY (person_id)
                    REFERENCES person(id)
            );

            CREATE TABLE person_specification (
                id INTEGER PRIMARY KEY,
                person_id INTEGER,
                specification_id INTEGER,
                FOREIGN KEY (person_id)
                    REFERENCES person(id)
            );

            CREATE TABLE rationale (
                id INTEGER PRIMARY KEY,
                details TEXT,
                person_spec_id INTEGER,
                rationale_type_id INTEGER,
                lent_id INTEGER,
                rationale_id INTEGER,
                date_start TEXT,
                date_end TEXT,
                function_type_id INTEGER,
                function_type_name TEXT,
                FOREIGN KEY (person_spec_id)
                    REFERENCES person_specification(id),
                FOREIGN KEY (rationale_id)
                    REFERENCES rationale(id)
            );

            CREATE TABLE address (
                id INTEGER PRIMARY KEY,
                street TEXT,
                rationale_id INTEGER,
                FOREIGN KEY (rationale_id)
                    REFERENCES rationale(id)
            );
            """
        )

        if reordered:
            person_id = 7
            person_spec_id = 20
            rationale_a_id = 200
            rationale_b_id = 100
            address_a_id = 2000
            address_b_id = 1000

        else:
            person_id = 1
            person_spec_id = 10
            rationale_a_id = 100
            rationale_b_id = 200
            address_a_id = 1000
            address_b_id = 2000

        connection.execute(
            "INSERT INTO person VALUES (?, ?)",
            (
                person_id,
                "Alice Example",
            ),
        )

        connection.execute(
            "INSERT INTO identifiers VALUES (?, ?, ?)",
            (
                person_id,
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                person_id,
            ),
        )

        connection.execute(
            "INSERT INTO person_specification VALUES (?, ?, ?)",
            (
                person_spec_id,
                person_id,
                1,
            ),
        )

        rationales = (
            (
                rationale_a_id,
                "Ground A",
            ),
            (
                rationale_b_id,
                second_details,
            ),
        )

        for rationale_id, details in rationales:
            connection.execute(
                "INSERT INTO rationale "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rationale_id,
                    details,
                    person_spec_id,
                    1,
                    None,
                    None,
                    "2026-10-20",
                    None,
                    None,
                    None,
                ),
            )

        address_a_rationale_id = (
            rationale_b_id
            if swap_addresses
            else rationale_a_id
        )

        address_b_rationale_id = (
            rationale_a_id
            if swap_addresses
            else rationale_b_id
        )

        connection.execute(
            "INSERT INTO address VALUES (?, ?, ?)",
            (
                address_a_id,
                "Address A",
                address_a_rationale_id,
            ),
        )

        connection.execute(
            "INSERT INTO address VALUES (?, ?, ?)",
            (
                address_b_id,
                "Address B",
                address_b_rationale_id,
            ),
        )

        connection.commit()

    finally:
        connection.close()


def test_duplicate_rationale_identity_ignores_generated_id_reordering(
    tmp_path,
):
    (
        runs,
        candidate,
        canonical_dir,
        canonical,
    ) = setup_paths(
        tmp_path
    )

    create_duplicate_rationale_database(
        canonical,
        reordered=False,
    )

    create_duplicate_rationale_database(
        candidate,
        reordered=True,
    )

    result = compare_feature(
        "41",
        runs,
        canonical_dir,
    )

    assert result.state == "SAME"

    summary = summarize_sqlite_comparison(
        candidate,
        canonical,
    )

    assert summary.tier == "SAME"
    assert summary.candidate_entity_count == 1
    assert summary.canonical_entity_count == 1
    assert summary.matched_entity_count == 1
    assert summary.changed_entity_count == 0
    assert summary.changed_tables == ()


def test_duplicate_rationale_still_detects_attached_address_reassignment(
    tmp_path,
):
    (
        runs,
        candidate,
        canonical_dir,
        canonical,
    ) = setup_paths(
        tmp_path
    )

    create_duplicate_rationale_database(
        canonical,
        reordered=False,
    )

    create_duplicate_rationale_database(
        candidate,
        reordered=True,
        swap_addresses=True,
    )

    result = compare_feature(
        "41",
        runs,
        canonical_dir,
    )

    assert result.state == "DIFFERENT"

    summary = summarize_sqlite_comparison(
        candidate,
        canonical,
    )

    assert summary.tier == "DATA CHANGED"
    assert summary.matched_entity_count == 1
    assert summary.changed_entity_count == 1

    assert (
        "address"
        in summary.changed_tables
    )


def test_duplicate_rationale_still_detects_rationale_content_change(
    tmp_path,
):
    (
        runs,
        candidate,
        canonical_dir,
        canonical,
    ) = setup_paths(
        tmp_path
    )

    create_duplicate_rationale_database(
        canonical,
        reordered=False,
        second_details="Ground B",
    )

    create_duplicate_rationale_database(
        candidate,
        reordered=True,
        second_details="Ground B updated",
    )

    result = compare_feature(
        "41",
        runs,
        canonical_dir,
    )

    assert result.state == "DIFFERENT"

    summary = summarize_sqlite_comparison(
        candidate,
        canonical,
    )

    assert summary.tier == "DATA CHANGED"
    assert summary.matched_entity_count == 1
    assert summary.changed_entity_count == 1

    assert (
        "rationale"
        in summary.changed_tables
    )

def create_duplicate_person_specification_database(
    path,
    *,
    reordered=False,
    second_rationale_details="Ground B",
):
    connection = sqlite3.connect(path)

    try:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE person (
                id INTEGER PRIMARY KEY,
                full_name TEXT
            );

            CREATE TABLE identifiers (
                id INTEGER PRIMARY KEY,
                value TEXT,
                person_id INTEGER,
                FOREIGN KEY (person_id)
                    REFERENCES person(id)
            );

            CREATE TABLE person_specification (
                id INTEGER PRIMARY KEY,
                person_id INTEGER,
                specification_id INTEGER,
                FOREIGN KEY (person_id)
                    REFERENCES person(id)
            );

            CREATE TABLE rationale (
                id INTEGER PRIMARY KEY,
                details TEXT,
                person_spec_id INTEGER,
                rationale_type_id INTEGER,
                lent_id INTEGER,
                rationale_id INTEGER,
                date_start TEXT,
                date_end TEXT,
                function_type_id INTEGER,
                function_type_name TEXT,
                FOREIGN KEY (person_spec_id)
                    REFERENCES person_specification(id),
                FOREIGN KEY (rationale_id)
                    REFERENCES rationale(id)
            );
            """
        )

        if reordered:
            person_id = 7
            person_spec_a_id = 20
            person_spec_b_id = 10
            rationale_a_id = 200
            rationale_b_id = 100

        else:
            person_id = 1
            person_spec_a_id = 10
            person_spec_b_id = 20
            rationale_a_id = 100
            rationale_b_id = 200

        connection.execute(
            "INSERT INTO person VALUES (?, ?)",
            (
                person_id,
                "Alice Example",
            ),
        )

        connection.execute(
            "INSERT INTO identifiers VALUES (?, ?, ?)",
            (
                person_id,
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                person_id,
            ),
        )

        for person_spec_id in (
            person_spec_a_id,
            person_spec_b_id,
        ):
            connection.execute(
                "INSERT INTO person_specification VALUES (?, ?, ?)",
                (
                    person_spec_id,
                    person_id,
                    1,
                ),
            )

        rationales = (
            (
                rationale_a_id,
                "Ground A",
                person_spec_a_id,
            ),
            (
                rationale_b_id,
                second_rationale_details,
                person_spec_b_id,
            ),
        )

        for (
            rationale_id,
            details,
            person_spec_id,
        ) in rationales:
            connection.execute(
                "INSERT INTO rationale "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rationale_id,
                    details,
                    person_spec_id,
                    1,
                    None,
                    None,
                    "2026-10-20",
                    None,
                    None,
                    None,
                ),
            )

        connection.commit()

    finally:
        connection.close()


def test_duplicate_person_specification_identity_ignores_generated_id_reordering(
    tmp_path,
):
    (
        runs,
        candidate,
        canonical_dir,
        canonical,
    ) = setup_paths(
        tmp_path
    )

    create_duplicate_person_specification_database(
        canonical,
        reordered=False,
    )

    create_duplicate_person_specification_database(
        candidate,
        reordered=True,
    )

    result = compare_feature(
        "41",
        runs,
        canonical_dir,
    )

    assert result.state == "SAME"

    summary = summarize_sqlite_comparison(
        candidate,
        canonical,
    )

    assert summary.tier == "SAME"
    assert summary.candidate_entity_count == 1
    assert summary.canonical_entity_count == 1
    assert summary.matched_entity_count == 1
    assert summary.changed_entity_count == 0
    assert summary.changed_tables == ()


def test_duplicate_person_specification_still_detects_child_rationale_change(
    tmp_path,
):
    (
        runs,
        candidate,
        canonical_dir,
        canonical,
    ) = setup_paths(
        tmp_path
    )

    create_duplicate_person_specification_database(
        canonical,
        reordered=False,
        second_rationale_details="Ground B",
    )

    create_duplicate_person_specification_database(
        candidate,
        reordered=True,
        second_rationale_details="Ground B updated",
    )

    result = compare_feature(
        "41",
        runs,
        canonical_dir,
    )

    assert result.state == "DIFFERENT"

    summary = summarize_sqlite_comparison(
        candidate,
        canonical,
    )

    assert summary.tier == "DATA CHANGED"
    assert summary.matched_entity_count == 1
    assert summary.changed_entity_count == 1

    assert (
        "rationale"
        in summary.changed_tables
    )


def create_address_type_identity_database(
    path,
    *,
    swapped_ids=False,
    reference_name="REFERENCE AREA",
):
    connection = sqlite3.connect(path)

    try:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE person (
                id INTEGER PRIMARY KEY,
                full_name TEXT
            );

            CREATE TABLE identifiers (
                id INTEGER PRIMARY KEY,
                value TEXT,
                person_id INTEGER,
                FOREIGN KEY (person_id)
                    REFERENCES person(id)
            );

            CREATE TABLE address_type (
                id INTEGER PRIMARY KEY,
                type_name TEXT
            );

            CREATE TABLE address (
                id INTEGER PRIMARY KEY,
                street TEXT,
                address_type_id INTEGER,
                person_id INTEGER,
                FOREIGN KEY (address_type_id)
                    REFERENCES address_type(id),
                FOREIGN KEY (person_id)
                    REFERENCES person(id)
            );
            """
        )

        connection.execute(
            "INSERT INTO person VALUES (?, ?)",
            (
                1,
                "Alice Example",
            ),
        )

        connection.execute(
            "INSERT INTO identifiers VALUES (?, ?, ?)",
            (
                1,
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                1,
            ),
        )

        if swapped_ids:
            legal_id = 4
            reference_id = 3

        else:
            legal_id = 3
            reference_id = 4

        connection.execute(
            "INSERT INTO address_type VALUES (?, ?)",
            (
                legal_id,
                "LEGAL ADDRESS",
            ),
        )

        connection.execute(
            "INSERT INTO address_type VALUES (?, ?)",
            (
                reference_id,
                reference_name,
            ),
        )

        connection.execute(
            "INSERT INTO address VALUES (?, ?, ?, ?)",
            (
                10,
                "Legal Street",
                legal_id,
                1,
            ),
        )

        connection.execute(
            "INSERT INTO address VALUES (?, ?, ?, ?)",
            (
                20,
                "Reference Street",
                reference_id,
                1,
            ),
        )

        connection.commit()

    finally:
        connection.close()


def test_address_type_generated_id_reordering_is_semantically_same(
    tmp_path,
):
    (
        runs,
        candidate,
        canonical_dir,
        canonical,
    ) = setup_paths(
        tmp_path
    )

    create_address_type_identity_database(
        canonical,
        swapped_ids=False,
    )

    create_address_type_identity_database(
        candidate,
        swapped_ids=True,
    )

    result = compare_feature(
        "41",
        runs,
        canonical_dir,
    )

    assert result.state == "SAME"

    summary = summarize_sqlite_comparison(
        candidate,
        canonical,
    )

    assert summary.tier == "SAME"
    assert summary.changed_entity_count == 0
    assert summary.changed_tables == ()


def test_address_type_semantic_name_change_is_still_detected(
    tmp_path,
):
    (
        runs,
        candidate,
        canonical_dir,
        canonical,
    ) = setup_paths(
        tmp_path
    )

    create_address_type_identity_database(
        canonical,
        swapped_ids=False,
        reference_name="REFERENCE AREA",
    )

    create_address_type_identity_database(
        candidate,
        swapped_ids=True,
        reference_name="REFERENCE AREA UPDATED",
    )

    result = compare_feature(
        "41",
        runs,
        canonical_dir,
    )

    assert result.state == "DIFFERENT"

    summary = summarize_sqlite_comparison(
        candidate,
        canonical,
    )

    assert summary.tier == "DATA CHANGED"
    assert summary.matched_entity_count == 1
    assert summary.changed_entity_count == 1

    assert (
        "address"
        in summary.changed_tables
    )

def test_duplicate_rationale_collision_resolution_scans_child_table_at_most_twice(
    tmp_path,
):
    database = tmp_path / "many-collisions.sqlite"
    connection = sqlite3.connect(database)

    try:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE person (
                id INTEGER PRIMARY KEY,
                full_name TEXT
            );

            CREATE TABLE identifiers (
                id INTEGER PRIMARY KEY,
                value TEXT,
                person_id INTEGER,
                FOREIGN KEY (person_id)
                    REFERENCES person(id)
            );

            CREATE TABLE person_specification (
                id INTEGER PRIMARY KEY,
                person_id INTEGER,
                specification_id INTEGER,
                FOREIGN KEY (person_id)
                    REFERENCES person(id)
            );

            CREATE TABLE rationale (
                id INTEGER PRIMARY KEY,
                details TEXT,
                person_spec_id INTEGER,
                rationale_type_id INTEGER,
                lent_id INTEGER,
                rationale_id INTEGER,
                date_start TEXT,
                date_end TEXT,
                function_type_id INTEGER,
                function_type_name TEXT,
                FOREIGN KEY (person_spec_id)
                    REFERENCES person_specification(id),
                FOREIGN KEY (rationale_id)
                    REFERENCES rationale(id)
            );

            CREATE TABLE address (
                id INTEGER PRIMARY KEY,
                street TEXT,
                rationale_id INTEGER,
                FOREIGN KEY (rationale_id)
                    REFERENCES rationale(id)
            );
            """
        )

        connection.execute(
            "INSERT INTO person VALUES (?, ?)",
            (
                1,
                "Alice Example",
            ),
        )

        connection.execute(
            "INSERT INTO identifiers VALUES (?, ?, ?)",
            (
                1,
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                1,
            ),
        )

        connection.execute(
            "INSERT INTO person_specification VALUES (?, ?, ?)",
            (
                10,
                1,
                1,
            ),
        )

        collision_count = 64

        for index in range(
            collision_count
        ):
            rationale_id = 1000 + index

            connection.execute(
                "INSERT INTO rationale "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rationale_id,
                    "Ground %d" % index,
                    10,
                    1,
                    None,
                    None,
                    "2026-10-20",
                    None,
                    None,
                    None,
                ),
            )

            connection.execute(
                "INSERT INTO address VALUES (?, ?, ?)",
                (
                    2000 + index,
                    "Address %d" % index,
                    rationale_id,
                ),
            )

        connection.commit()

        traced_statements = []
        connection.set_trace_callback(
            traced_statements.append
        )

        identity_context = (
            sqlite_worker._build_identity_context(
                connection
            )
        )

        connection.set_trace_callback(
            None
        )

        rationale_tokens = (
            identity_context[
                "stable_ids"
            ][
                "rationale"
            ]
        )

        assert len(
            rationale_tokens
        ) == collision_count

        assert len(
            set(
                rationale_tokens.values()
            )
        ) == collision_count

        address_selects = [
            statement
            for statement
            in traced_statements
            if (
                'SELECT * FROM "address"'
                in statement
            )
        ]

        assert len(
            address_selects
        ) <= 2

    finally:
        connection.close()

@pytest.mark.parametrize("feature", ["../41", "٤١", "abc"])
def test_comparison_rejects_invalid_feature_numbers(tmp_path, feature):
    with pytest.raises(ComparisonError):
        compare_feature(feature, tmp_path)

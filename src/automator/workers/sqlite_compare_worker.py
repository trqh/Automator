"""Read-only logical SQLite comparison and explicit canonical replacement."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple
from datetime import date, datetime
from uuid import uuid4

DIFF_EXAMPLE_LIMIT = 10
DIFF_VALUE_LIMIT = 240
_IGNORED_VALUE_COLUMNS = {("specification", "list_update")}
_IMPORT_CODE_DATE_SUFFIX = re.compile(r"-\d{4}\.\d{2}\.\d{2}$")


class ComparisonError(RuntimeError):
    """Raised when comparison or safe replacement cannot complete."""


@dataclass(frozen=True)
class SQLiteComparisonSummary:
    """Structured semantic evidence used to classify one SQLite comparison."""

    tier: str
    candidate_entity_count: int
    canonical_entity_count: int
    matched_entity_count: int
    added_identifiers: Tuple[str, ...] = ()
    removed_identifiers: Tuple[str, ...] = ()
    changed_entity_count: int = 0
    changed_identifiers: Tuple[str, ...] = ()
    changed_tables: Tuple[str, ...] = ()
    schema_changes: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ComparisonResult:
    feature_number: str
    state: str
    candidate: Path
    canonical: Path
    candidate_sha256: Optional[str] = None
    canonical_sha256: Optional[str] = None
    message: str = ""
    candidate_logical_sha256: Optional[str] = None
    canonical_logical_sha256: Optional[str] = None
    byte_identical: Optional[bool] = None
    summary: Optional[SQLiteComparisonSummary] = None


@dataclass(frozen=True)
class TableDifference:
    """A bounded, read-only comparison result for one SQLite table."""

    table_name: str
    state: str
    candidate_rows: Optional[int]
    canonical_rows: Optional[int]
    primary_key: Tuple[str, ...] = ()
    message: str = ""
    examples: Tuple[Dict[str, Any], ...] = ()


@dataclass(frozen=True)
class SQLiteDifferenceReport:
    """Bounded semantic differences between a candidate and canonical SQLite file."""

    candidate: Path
    canonical: Path
    table_differences: Tuple[TableDifference, ...]
    message: str = ""


def _feature(value: str) -> str:
    value = str(value)
    if not value.isascii() or not value.isdigit():
        raise ComparisonError("Feature number must contain ASCII digits only.")
    return value


def sha256_file(path: Path | str, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_history_path(canonical: Path) -> Path:
    feature_number = canonical.stem
    history_dir = canonical.parent.parent / "Canonical Output History" / feature_number
    dated = history_dir / (feature_number + "_" + date.today().isoformat() + canonical.suffix)
    if not dated.exists():
        return dated
    # Retain the first date-only snapshot for backwards compatibility.
    # Every further acceptance gets its own collision-resistant filename.
    return history_dir / (
        feature_number + "_" + datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")
        + "_" + uuid4().hex[:12] + canonical.suffix
    )


def _archive_existing_canonical(canonical: Path) -> Optional[Path]:
    if not canonical.is_file():
        return None

    archive = _canonical_history_path(canonical)
    archive.parent.mkdir(parents=True, exist_ok=True)

    if archive.exists():
        raise ComparisonError(
            "History archive path already exists; refusing to overwrite or skip: %s"
            % archive
        )

    canonical_digest = sha256_file(canonical)
    temp_name = None
    archive_created = False

    try:
        with tempfile.NamedTemporaryFile(
            prefix="." + archive.name + ".",
            suffix=".tmp",
            dir=str(archive.parent),
            delete=False,
        ) as target:
            temp_name = target.name

            with canonical.open("rb") as input_stream:
                for chunk in iter(
                    lambda: input_stream.read(1024 * 1024),
                    b"",
                ):
                    target.write(chunk)

            target.flush()
            os.fsync(target.fileno())

        if (
            sha256_file(temp_name) != canonical_digest
            or sha256_file(canonical) != canonical_digest
        ):
            raise ComparisonError(
                "Canonical changed while history was being preserved; "
                "canonical file was not replaced."
            )

        try:
            os.link(temp_name, archive)
            archive_created = True
        except FileExistsError as exc:
            raise ComparisonError(
                "Concurrent history archive creation detected; canonical unchanged: %s"
                % archive
            ) from exc

        if sha256_file(archive) != canonical_digest:
            if archive_created:
                try:
                    archive.unlink()
                except OSError:
                    pass

            raise ComparisonError(
                "Archived canonical verification failed; "
                "canonical file was not replaced."
            )

        return archive

    except ComparisonError:
        raise
    except OSError as exc:
        raise ComparisonError(
            "Could not archive canonical file: %s" % exc
        ) from exc
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def safe_replace_from_candidate(candidate: Path | str, canonical: Path | str) -> str:
    """Preserve any existing canonical, then safely replace it from candidate."""
    source = Path(candidate)
    destination = Path(canonical)

    if not source.is_file():
        raise ComparisonError("Candidate file is missing: %s" % source)

    destination.parent.mkdir(parents=True, exist_ok=True)
    _archive_existing_canonical(destination)

    source_digest = sha256_file(source)
    temp_name = None

    try:
        with tempfile.NamedTemporaryFile(
            prefix="." + destination.name + ".",
            suffix=".tmp",
            dir=str(destination.parent),
            delete=False,
        ) as target:
            temp_name = target.name

            with source.open("rb") as input_stream:
                for chunk in iter(
                    lambda: input_stream.read(1024 * 1024),
                    b"",
                ):
                    target.write(chunk)

            target.flush()
            os.fsync(target.fileno())

        if (
            sha256_file(temp_name) != source_digest
            or sha256_file(source) != source_digest
        ):
            raise ComparisonError(
                "Candidate changed during copy; canonical file was not replaced."
            )

        os.replace(temp_name, destination)
        temp_name = None
        return source_digest

    except OSError as exc:
        raise ComparisonError(
            "Could not safely replace canonical file: %s" % exc
        ) from exc
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def _open_read_only_database(path: Path | str) -> sqlite3.Connection:
    database_path = Path(path).resolve()
    if not database_path.is_file():
        raise ComparisonError("SQLite file is missing: %s" % database_path)
    connection = sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    return connection


def _normalized_comparison_value(table_name: str, column_name: str, value: Any) -> Any:
    """Apply the documented, narrowly scoped volatile-field comparison policy."""
    key = (table_name.casefold(), column_name.casefold())
    if key in _IGNORED_VALUE_COLUMNS:
        return None
    if key == ("specification", "import_code") and isinstance(value, str):
        return _IMPORT_CODE_DATE_SUFFIX.sub("", value)
    return value


def _update_digest_value(digest, value: Any) -> None:
    """Add one typed, length-prefixed SQLite value to a logical digest."""
    if value is None:
        encoded = b"N"
    elif isinstance(value, bytes):
        encoded = b"B" + value
    elif isinstance(value, str):
        encoded = b"T" + value.encode("utf-8")
    elif isinstance(value, int):
        encoded = b"I" + str(value).encode("ascii")
    elif isinstance(value, float):
        encoded = b"F" + value.hex().encode("ascii")
    else:
        raise ComparisonError("Unsupported SQLite value type: %s" % type(value).__name__)
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)

def _table_foreign_keys(
    connection: sqlite3.Connection,
    table_name: str,
) -> Dict[str, Tuple[str, str]]:
    rows = connection.execute(
        "PRAGMA foreign_key_list(%s)" % _quote_identifier(table_name)
    )
    return {
        row[3]: (row[2], row[4])
        for row in rows
    }


def _stable_identity_token(
    table_name: str,
    values: Iterable[Tuple[str, Any]],
) -> str:
    digest = hashlib.sha256()
    digest.update(b"sqlite-stable-identity-v1")

    _update_digest_value(digest, table_name)

    for column_name, value in values:
        _update_digest_value(digest, column_name)
        _update_digest_value(digest, value)

    return "@%s:%s" % (
        table_name,
        digest.hexdigest(),
    )

def _build_rationale_collision_child_row_index(
    connection: sqlite3.Connection,
    rationale_ids: set[Any],
    rationale_primary_key: Tuple[str, ...],
    foreign_keys: Dict[str, Dict[str, Tuple[str, str]]],
) -> Dict[
    Tuple[str, str],
    Tuple[
        Tuple[str, ...],
        Tuple[str, ...],
        Dict[Any, Tuple[Tuple[Any, ...], ...]],
    ],
]:
    """Index child rows for collided rationales with one scan per child table."""
    if len(rationale_primary_key) != 1:
        raise ComparisonError(
            "Duplicate rationale identities require one surrogate primary key."
        )

    rationale_primary_key_column = rationale_primary_key[0]
    result = {}

    for child_table in sorted(foreign_keys):
        child_foreign_keys = foreign_keys[child_table]
        rationale_columns = tuple(sorted(
            column_name
            for column_name, (target_table, target_column)
            in child_foreign_keys.items()
            if (
                target_table == "rationale"
                and target_column == rationale_primary_key_column
            )
        ))

        if not rationale_columns:
            continue

        child_columns, child_primary_key = _columns_and_primary_key(
            connection,
            child_table,
        )

        column_indexes = {
            column_name: index
            for index, column_name in enumerate(child_columns)
        }

        rows_by_column: Dict[
            str,
            Dict[Any, list[Tuple[Any, ...]]],
        ] = {
            column_name: {}
            for column_name in rationale_columns
        }

        query = (
            "SELECT * FROM %s"
            % _quote_identifier(
                child_table
            )
        )

        for child_row in connection.execute(query):
            for child_rationale_column in rationale_columns:
                rationale_id = child_row[
                    column_indexes[
                        child_rationale_column
                    ]
                ]

                if rationale_id not in rationale_ids:
                    continue

                rows_by_column[
                    child_rationale_column
                ].setdefault(
                    rationale_id,
                    [],
                ).append(
                    child_row
                )

        for child_rationale_column in rationale_columns:
            result[
                (
                    child_table,
                    child_rationale_column,
                )
            ] = (
                child_columns,
                child_primary_key,
                {
                    rationale_id: tuple(rows)
                    for rationale_id, rows
                    in rows_by_column[
                        child_rationale_column
                    ].items()
                },
            )

    return result

def _rationale_collision_discriminator(
    connection: sqlite3.Connection,
    rationale_id: Any,
    rationale_columns: Tuple[str, ...],
    rationale_primary_key: Tuple[str, ...],
    rationale_row: Tuple[Any, ...],
    foreign_keys: Dict[
        str,
        Dict[str, Tuple[str, str]],
    ],
    stable_ids: Dict[
        str,
        Dict[Any, str],
    ],
    derived_tables: set[str],
    child_row_index: Dict[
        Tuple[str, str],
        Tuple[
            Tuple[str, ...],
            Tuple[str, ...],
            Dict[Any, Tuple[Tuple[Any, ...], ...]],
        ],
    ],
) -> str:
    """
    Build a deterministic discriminator for one rationale only when multiple
    rationale rows share the same relationship identity.

    The discriminator includes the rationale's mutable semantic payload and
    the semantic multiset of rows directly attached to that rationale. This
    keeps generated SQLite IDs irrelevant while preserving which child data
    belongs to which otherwise-ambiguous rationale.
    """
    if len(rationale_primary_key) != 1:
        raise ComparisonError(
            "Duplicate rationale identities require one surrogate primary key."
        )

    digest = hashlib.sha256()
    digest.update(
        b"sqlite-rationale-collision-v1"
    )

    rationale_by_column = dict(
        zip(
            rationale_columns,
            rationale_row,
        )
    )

    rationale_foreign_keys = foreign_keys.get(
        "rationale",
        {},
    )

    for column_name in rationale_columns:
        if column_name in rationale_primary_key:
            continue

        value = rationale_by_column[
            column_name
        ]

        foreign_key = rationale_foreign_keys.get(
            column_name
        )

        if (
            foreign_key is not None
            and value is not None
        ):
            (
                target_table,
                _target_column,
            ) = foreign_key

            if target_table == "rationale":
                raise ComparisonError(
                    "Duplicate rationale identity contains a "
                    "self-referential rationale link; automatic "
                    "collision resolution is not safe."
                )

            if (
                    target_table in derived_tables
                    or target_table in stable_ids
            ):
                target_map = stable_ids.get(
                    target_table
                )

                if (
                    target_map is None
                    or value not in target_map
                ):
                    raise ComparisonError(
                        "Could not resolve stable identity for "
                        "rationale.%s=%r while resolving a "
                        "duplicate rationale identity."
                        % (
                            column_name,
                            value,
                        )
                    )

                value = target_map[
                    value
                ]

        value = _normalized_comparison_value(
            "rationale",
            column_name,
            value,
        )

        _update_digest_value(
            digest,
            column_name,
        )
        _update_digest_value(
            digest,
            value,
        )

    for child_table in sorted(
        foreign_keys
    ):
        child_foreign_keys = foreign_keys[
            child_table
        ]

        rationale_columns_in_child = [
            column_name
            for (
                column_name,
                (
                    target_table,
                    target_column,
                ),
            )
            in child_foreign_keys.items()
            if (
                target_table == "rationale"
                and target_column
                == rationale_primary_key[0]
            )
        ]

        for child_rationale_column in sorted(
            rationale_columns_in_child
        ):
            (
                child_columns,
                child_primary_key,
                rows_by_rationale,
            ) = child_row_index[
                (
                    child_table,
                    child_rationale_column,
                )
            ]

            child_hashes = []

            for child_row in rows_by_rationale.get(
                    rationale_id,
                    (),
            ):
                child_digest = hashlib.sha256()
                child_digest.update(
                    b"sqlite-rationale-child-v1"
                )

                child_by_column = dict(
                    zip(
                        child_columns,
                        child_row,
                    )
                )

                for column_name in child_columns:
                    value = child_by_column[
                        column_name
                    ]

                    if (
                        len(child_primary_key) == 1
                        and column_name
                        == child_primary_key[0]
                    ):
                        value = None

                    elif (
                        column_name
                        == child_rationale_column
                    ):
                        value = "@rationale:self"

                    else:
                        foreign_key = (
                            child_foreign_keys.get(
                                column_name
                            )
                        )

                        if (
                            foreign_key is not None
                            and value is not None
                        ):
                            (
                                target_table,
                                _target_column,
                            ) = foreign_key

                            if (
                                target_table
                                == "rationale"
                            ):
                                raise ComparisonError(
                                    "A child row of a duplicate rationale "
                                    "contains another rationale foreign key; "
                                    "automatic collision resolution is not safe."
                                )

                            if (
                                    target_table in derived_tables
                                    or target_table in stable_ids
                            ):
                                target_map = stable_ids.get(
                                    target_table
                                )

                                if (
                                    target_map is None
                                    or value not in target_map
                                ):
                                    raise ComparisonError(
                                        "Could not resolve stable identity "
                                        "for %s.%s=%r while resolving a "
                                        "duplicate rationale identity."
                                        % (
                                            child_table,
                                            column_name,
                                            value,
                                        )
                                    )

                                value = target_map[
                                    value
                                ]

                    value = _normalized_comparison_value(
                        child_table,
                        column_name,
                        value,
                    )

                    _update_digest_value(
                        child_digest,
                        column_name,
                    )
                    _update_digest_value(
                        child_digest,
                        value,
                    )

                child_hashes.append(
                    child_digest.digest()
                )

            _update_digest_value(
                digest,
                child_table,
            )
            _update_digest_value(
                digest,
                child_rationale_column,
            )
            _update_digest_value(
                digest,
                len(child_hashes),
            )

            for child_hash in sorted(
                child_hashes
            ):
                digest.update(
                    child_hash
                )

    return digest.hexdigest()

def _build_identity_context(
    connection: sqlite3.Connection,
) -> Dict[str, Any]:
    """
    Build stable person-centred identities for generated surrogate IDs.

    SGRETL person IDs are insertion-order database IDs. The non-IMG value in
    identifiers is the stable semantic identity for a person, so tables
    connected to person through foreign keys are canonicalized around that
    value instead of their generated integer IDs.
    """
    tables = set(
        _user_table_names(connection)
    )

    foreign_keys = {
        table: _table_foreign_keys(
            connection,
            table,
        )
        for table in tables
    }

    empty = {
        "derived_tables": frozenset(),
        "foreign_keys": foreign_keys,
        "stable_ids": {},
    }

    if (
        "person" not in tables
        and "identifiers" not in tables
    ):
        return empty

    if (
        "person" not in tables
        or "identifiers" not in tables
    ):
        raise ComparisonError(
            "Person identity schema is incomplete; "
            "both person and identifiers are required."
        )

    _person_columns, person_primary_key = (
        _columns_and_primary_key(
            connection,
            "person",
        )
    )

    identifier_columns, _ = (
        _columns_and_primary_key(
            connection,
            "identifiers",
        )
    )

    if (
        person_primary_key != ("id",)
        or not {"person_id", "value"}.issubset(
            identifier_columns
        )
    ):
        raise ComparisonError(
            "Person identity schema is not supported by "
            "the stable-identifier comparator."
        )

    identifiers_by_person: Dict[
        Any,
        list[str],
    ] = {}

    for person_id, value in connection.execute(
        "SELECT person_id, value "
        "FROM identifiers "
        "ORDER BY person_id, value"
    ):
        if value is None:
            continue

        text = str(value)

        if text.startswith("IMG:"):
            continue

        identifiers_by_person.setdefault(
            person_id,
            [],
        ).append(text)

    person_ids = tuple(
        row[0]
        for row in connection.execute(
            "SELECT id FROM person ORDER BY id"
        )
    )

    person_map: Dict[Any, str] = {}
    reverse: Dict[str, Any] = {}

    for person_id in person_ids:
        values = sorted(
            set(
                identifiers_by_person.get(
                    person_id,
                    (),
                )
            )
        )

        if len(values) != 1:
            raise ComparisonError(
                "Person %s has %d non-image identifiers; "
                "exactly one stable identifier is required."
                % (
                    person_id,
                    len(values),
                )
            )

        identifier = values[0]

        previous = reverse.get(
            identifier
        )

        if (
            previous is not None
            and previous != person_id
        ):
            raise ComparisonError(
                "Stable person identifier is assigned "
                "to more than one person: %s"
                % identifier
            )

        reverse[identifier] = person_id

        person_map[person_id] = (
            "@person:%s" % identifier
        )
    # address_type.id is storage-generated. Normalize it by its semantic
    # type name so insertion-order changes do not alter address identity.
    address_type_map: Dict[
        Any,
        str,
    ] = {}

    if "address_type" in tables:
        (
            address_type_columns,
            address_type_primary_key,
        ) = _columns_and_primary_key(
            connection,
            "address_type",
        )

        if (
            address_type_primary_key
            == ("id",)
            and "type_name"
            in address_type_columns
        ):
            seen_address_types: Dict[
                str,
                Any,
            ] = {}

            for (
                raw_id,
                type_name,
            ) in connection.execute(
                "SELECT id, type_name "
                "FROM address_type"
            ):
                token = (
                    _stable_identity_token(
                        "address_type",
                        (
                            (
                                "type_name",
                                type_name,
                            ),
                        ),
                    )
                )

                previous = (
                    seen_address_types.get(
                        token
                    )
                )

                if (
                    previous is not None
                    and previous != raw_id
                ):
                    raise ComparisonError(
                        "Ambiguous stable identity "
                        "in table address_type."
                    )

                seen_address_types[
                    token
                ] = raw_id

                address_type_map[
                    raw_id
                ] = token
    # Discover all tables whose identity ultimately depends on person.id.
    derived = {"person"}

    changed = True

    while changed:
        changed = False

        for (
            table_name,
            table_foreign_keys,
        ) in foreign_keys.items():
            if table_name in derived:
                continue

            if any(
                target_table in derived
                for (
                    target_table,
                    _target_column,
                ) in table_foreign_keys.values()
            ):
                derived.add(
                    table_name
                )
                changed = True

    stable_ids: Dict[
        str,
        Dict[Any, str],
    ] = {
        "person": person_map,
    }

    if address_type_map:
        stable_ids[
            "address_type"
        ] = address_type_map

    remaining = (
        set(derived)
        - {"person"}
    )

    while remaining:
        progressed = False

        for table_name in sorted(
            tuple(remaining)
        ):
            (
                columns,
                primary_key,
            ) = _columns_and_primary_key(
                connection,
                table_name,
            )

            # Tables without one surrogate primary key can still have
            # their foreign keys canonicalized, but they do not need
            # their own stable ID map.
            if len(primary_key) != 1:
                remaining.remove(
                    table_name
                )
                progressed = True
                continue

            table_foreign_keys = (
                foreign_keys.get(
                    table_name,
                    {},
                )
            )

            unresolved = {
                target_table
                for (
                    _column,
                    (
                        target_table,
                        _target_column,
                    ),
                ) in table_foreign_keys.items()
                if (
                    target_table in derived
                    and target_table != table_name
                    and target_table
                    not in stable_ids
                )
            }

            if unresolved:
                continue

            primary_key_column = (
                primary_key[0]
            )

            identity_map: Dict[
                Any,
                str,
            ] = {}

            token_owner: Dict[
                str,
                Any,
            ] = {}

            rationale_token_owners: Dict[
                str,
                list[Any],
            ] = {}

            rationale_rows: Dict[
                Any,
                Tuple[Any, ...],
            ] = {}

            query = (
                "SELECT * FROM %s"
                % _quote_identifier(
                    table_name
                )
            )

            for row in connection.execute(
                query
            ):
                row_by_column = dict(
                    zip(
                        columns,
                        row,
                    )
                )

                raw_id = row_by_column[
                    primary_key_column
                ]

                identity_values = []

                for column_name in columns:
                    if (
                        column_name
                        == primary_key_column
                    ):
                        continue

                    # Rationale details and dates are mutable content,
                    # not the rationale's relationship identity.
                    # Excluding them here prevents a genuine rationale
                    # change from making unchanged links look different.
                    if (
                        table_name
                        == "rationale"
                        and column_name
                        in {
                            "details",
                            "date_start",
                            "date_end",
                            "rationale_id",
                        }
                    ):
                        continue

                    value = row_by_column[
                        column_name
                    ]

                    foreign_key = (
                        table_foreign_keys.get(
                            column_name
                        )
                    )

                    if (
                        foreign_key
                        is not None
                        and value
                        is not None
                    ):
                        (
                            target_table,
                            _target_column,
                        ) = foreign_key

                        if (
                            target_table
                            == table_name
                        ):
                            value = (
                                "@self-reference"
                            )


                        elif (

                                target_table

                                in derived

                                or target_table

                                in stable_ids

                        ):
                            target_map = (
                                stable_ids.get(
                                    target_table
                                )
                            )

                            if (
                                target_map
                                is None
                                or value
                                not in target_map
                            ):
                                raise ComparisonError(
                                    "Could not resolve stable identity "
                                    "for %s.%s=%r."
                                    % (
                                        table_name,
                                        column_name,
                                        value,
                                    )
                                )

                            value = (
                                target_map[
                                    value
                                ]
                            )

                    value = (
                        _normalized_comparison_value(
                            table_name,
                            column_name,
                            value,
                        )
                    )

                    identity_values.append(
                        (
                            column_name,
                            value,
                        )
                    )

                token = (
                    _stable_identity_token(
                        table_name,
                        identity_values,
                    )
                )

                # Multiple raw rows can legitimately represent the same
                # person/specification relationship after SGRETL merges
                # duplicate persons. They are semantically interchangeable,
                # so all raw IDs intentionally map to the same stable token.
                if (
                        table_name
                        == "person_specification"
                ):
                    token_owner.setdefault(
                        token,
                        raw_id,
                    )

                elif table_name == "rationale":
                    rationale_token_owners.setdefault(
                        token,
                        [],
                    ).append(
                        raw_id
                    )

                    rationale_rows[
                        raw_id
                    ] = row

                identity_map[
                    raw_id
                ] = token

            if table_name == "rationale":
                collision_ids = {
                    raw_id
                    for owners
                    in rationale_token_owners.values()
                    if len(owners) >= 2
                    for raw_id in owners
                }

                child_row_index = (
                    _build_rationale_collision_child_row_index(
                        connection,
                        collision_ids,
                        primary_key,
                        foreign_keys,
                    )
                    if collision_ids
                    else {}
                )

                for (
                        base_token,
                        owners,
                ) in rationale_token_owners.items():
                    if len(owners) < 2:
                        continue

                    for raw_id in owners:
                        discriminator = (
                            _rationale_collision_discriminator(
                                connection,
                                raw_id,
                                columns,
                                primary_key,
                                rationale_rows[
                                    raw_id
                                ],
                                foreign_keys,
                                stable_ids,
                                derived,
                                child_row_index,
                            )
                        )

                        identity_map[
                            raw_id
                        ] = (
                                base_token
                                + ":collision:"
                                + discriminator
                        )

            stable_ids[
                table_name
            ] = identity_map

            remaining.remove(
                table_name
            )

            progressed = True

        if not progressed:
            raise ComparisonError(
                "Could not resolve stable identity dependencies for: %s"
                % ", ".join(
                    sorted(
                        remaining
                    )
                )
            )

    return {
        "derived_tables": frozenset(
            derived
        ),
        "foreign_keys": foreign_keys,
        "stable_ids": stable_ids,
    }


def _canonicalize_row(
    table_name: str,
    columns: Tuple[str, ...],
    primary_key: Tuple[str, ...],
    row: Tuple[Any, ...],
    identity_context: Dict[str, Any],
    *,
    normalize_volatile: bool,
) -> Tuple[Any, ...]:
    derived_tables = (
        identity_context[
            "derived_tables"
        ]
    )

    foreign_keys = (
        identity_context[
            "foreign_keys"
        ].get(
            table_name,
            {},
        )
    )

    stable_ids = (
        identity_context[
            "stable_ids"
        ]
    )

    canonical = []

    for (
        column_name,
        raw_value,
    ) in zip(
        columns,
        row,
    ):
        value = raw_value

        if (
            table_name == "person"
            and column_name
            in primary_key
            and value is not None
        ):
            value = (
                stable_ids[
                    "person"
                ].get(
                    value
                )
            )

            if value is None:
                raise ComparisonError(
                    "Could not resolve stable person identifier "
                    "for person.id=%r."
                    % raw_value
                )
        elif (
            table_name
            == "address_type"
            and column_name
            in primary_key
            and value is not None
        ):
            value = stable_ids[
                "address_type"
            ].get(
                value
            )

            if value is None:
                raise ComparisonError(
                    "Could not resolve stable address type identifier "
                    "for address_type.id=%r."
                    % raw_value
                )

        elif (
            table_name
            in derived_tables
            and len(
                primary_key
            ) == 1
            and column_name
            == primary_key[0]
        ):
            # Storage-generated IDs have no semantic meaning.
            value = None

        else:
            foreign_key = (
                foreign_keys.get(
                    column_name
                )
            )

            if (
                foreign_key
                is not None
                and value
                is not None
            ):
                (
                    target_table,
                    _target_column,
                ) = foreign_key

                if (
                        target_table in derived_tables
                        or target_table in stable_ids
                ):
                    target_map = (
                        stable_ids.get(
                            target_table
                        )
                    )

                    if (
                        target_map
                        is None
                        or value
                        not in target_map
                    ):
                        raise ComparisonError(
                            "Could not resolve stable foreign key "
                            "for %s.%s=%r."
                            % (
                                table_name,
                                column_name,
                                value,
                            )
                        )

                    value = (
                        target_map[
                            value
                        ]
                    )

        if normalize_volatile:
            value = (
                _normalized_comparison_value(
                    table_name,
                    column_name,
                    value,
                )
            )

        canonical.append(
            value
        )

    return tuple(
        canonical
    )


def _row_digest(
    table_name: str,
    columns: Tuple[str, ...],
    row: Tuple[Any, ...],
) -> bytes:
    digest = hashlib.sha256()

    digest.update(
        b"sqlite-logical-row-v2"
    )

    _update_digest_value(
        digest,
        table_name,
    )

    for (
        column_name,
        value,
    ) in zip(
        columns,
        row,
    ):
        _update_digest_value(
            digest,
            column_name,
        )

        _update_digest_value(
            digest,
            value,
        )

    return digest.digest()


def logical_sha256_file(
    path: Path | str,
) -> str:
    """
    Hash semantic SQLite contents while ignoring generated
    person-order IDs.
    """
    connection = (
        _open_read_only_database(
            path
        )
    )

    digest = hashlib.sha256()

    try:
        digest.update(
            b"sqlite-logical-comparison-v2"
        )

        identity_context = (
            _build_identity_context(
                connection
            )
        )

        schema_rows = (
            connection.execute(
                "SELECT type, name, tbl_name, COALESCE(sql, '') "
                "FROM sqlite_master "
                "WHERE type IN ('table', 'index', 'view', 'trigger') "
                "AND name NOT LIKE 'sqlite_%' "
                "ORDER BY type, name, tbl_name, sql"
            )
        )

        for schema_row in schema_rows:
            digest.update(
                b"S"
            )

            for value in schema_row:
                _update_digest_value(
                    digest,
                    value,
                )

        for table_name in (
            _user_table_names(
                connection
            )
        ):
            (
                columns,
                primary_key,
            ) = _columns_and_primary_key(
                connection,
                table_name,
            )

            digest.update(
                b"T"
            )

            _update_digest_value(
                digest,
                table_name,
            )

            _update_digest_value(
                digest,
                len(columns),
            )

            for column_name in columns:
                _update_digest_value(
                    digest,
                    column_name,
                )

            row_digests = []

            query = (
                "SELECT * FROM %s"
                % _quote_identifier(
                    table_name
                )
            )

            for row in (
                connection.execute(
                    query
                )
            ):
                canonical_row = (
                    _canonicalize_row(
                        table_name,
                        columns,
                        primary_key,
                        row,
                        identity_context,
                        normalize_volatile=True,
                    )
                )

                row_digests.append(
                    _row_digest(
                        table_name,
                        columns,
                        canonical_row,
                    )
                )

            # Sorting semantic row hashes means database insertion
            # order has no effect on the database-level digest.
            for row_digest in sorted(
                row_digests
            ):
                digest.update(
                    b"R"
                )

                digest.update(
                    row_digest
                )

        return digest.hexdigest()

    except sqlite3.Error as exc:
        raise ComparisonError(
            "Could not calculate logical SQLite digest: %s"
            % exc
        ) from exc

    finally:
        connection.close()

def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _user_table_names(connection: sqlite3.Connection) -> Tuple[str, ...]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    return tuple(row[0] for row in rows)


def _schema_signature(connection: sqlite3.Connection, table_name: str) -> Tuple[Tuple[str, str, str], ...]:
    rows = connection.execute(
        "SELECT type, name, COALESCE(sql, '') FROM sqlite_master "
        "WHERE tbl_name = ? AND type IN ('table', 'index', 'trigger') "
        "AND name NOT LIKE 'sqlite_autoindex_%' ORDER BY type, name",
        (table_name,),
    )
    return tuple((row[0], row[1], row[2]) for row in rows)


def _columns_and_primary_key(connection: sqlite3.Connection, table_name: str) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    rows = tuple(connection.execute("PRAGMA table_info(%s)" % _quote_identifier(table_name)))
    columns = tuple(row[1] for row in rows)
    primary_key = tuple(row[1] for row in sorted(rows, key=lambda row: row[5]) if row[5] > 0)
    return columns, primary_key


def _row_count(connection: sqlite3.Connection, table_name: str) -> int:
    return int(connection.execute("SELECT COUNT(*) FROM %s" % _quote_identifier(table_name)).fetchone()[0])


def _display_value(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        preview = value[:DIFF_VALUE_LIMIT]
        suffix = "..." if len(value) > DIFF_VALUE_LIMIT else ""
        return "BLOB(%d): %s%s" % (len(value), preview.hex(), suffix)
    text = str(value)
    if len(text) > DIFF_VALUE_LIMIT:
        return text[:DIFF_VALUE_LIMIT] + "..."
    return text


def _display_row(columns: Iterable[str], row: Tuple[Any, ...]) -> Dict[str, str]:
    return {column: _display_value(value) for column, value in zip(columns, row)}


def _key_sort_value(value: Any) -> Tuple[int, Any]:
    if value is None:
        return (0, "")
    if isinstance(value, (int, float)):
        return (1, value)
    if isinstance(value, str):
        return (2, value)
    if isinstance(value, bytes):
        return (3, value)
    return (4, str(value))


def _row_key(row: Tuple[Any, ...], primary_key_indexes: Tuple[int, ...]) -> Tuple[Tuple[int, Any], ...]:
    return tuple(_key_sort_value(row[index]) for index in primary_key_indexes)


def _key_example(primary_key: Tuple[str, ...], row: Tuple[Any, ...], primary_key_indexes: Tuple[int, ...]) -> Dict[str, str]:
    return {
        column: _display_value(row[index])
        for column, index in zip(primary_key, primary_key_indexes)
    }


def _read_next(rows):
    try:
        return next(rows)
    except StopIteration:
        return None


def _compare_keyed_rows(candidate: sqlite3.Connection, canonical: sqlite3.Connection,
                        table_name: str, columns: Tuple[str, ...], primary_key: Tuple[str, ...],
                        example_limit: int) -> Tuple[str, Tuple[Dict[str, Any], ...], str]:
    primary_key_indexes = tuple(columns.index(column) for column in primary_key)
    order = ", ".join("%s COLLATE BINARY" % _quote_identifier(column) for column in primary_key)
    query = "SELECT * FROM %s ORDER BY %s" % (_quote_identifier(table_name), order)
    candidate_rows = iter(candidate.execute(query))
    canonical_rows = iter(canonical.execute(query))
    candidate_row = _read_next(candidate_rows)
    canonical_row = _read_next(canonical_rows)
    examples = []
    added = removed = changed = 0

    while candidate_row is not None or canonical_row is not None:
        if canonical_row is None:
            added += 1
            if len(examples) < example_limit:
                examples.append({"kind": "added", "key": _key_example(primary_key, candidate_row, primary_key_indexes),
                                 "candidate": _display_row(columns, candidate_row)})
            candidate_row = _read_next(candidate_rows)
            continue
        if candidate_row is None:
            removed += 1
            if len(examples) < example_limit:
                examples.append({"kind": "removed", "key": _key_example(primary_key, canonical_row, primary_key_indexes),
                                 "canonical": _display_row(columns, canonical_row)})
            canonical_row = _read_next(canonical_rows)
            continue

        candidate_key = _row_key(candidate_row, primary_key_indexes)
        canonical_key = _row_key(canonical_row, primary_key_indexes)
        if candidate_key < canonical_key:
            added += 1
            if len(examples) < example_limit:
                examples.append({"kind": "added", "key": _key_example(primary_key, candidate_row, primary_key_indexes),
                                 "candidate": _display_row(columns, candidate_row)})
            candidate_row = _read_next(candidate_rows)
        elif candidate_key > canonical_key:
            removed += 1
            if len(examples) < example_limit:
                examples.append({"kind": "removed", "key": _key_example(primary_key, canonical_row, primary_key_indexes),
                                 "canonical": _display_row(columns, canonical_row)})
            canonical_row = _read_next(canonical_rows)
        else:
            if candidate_row != canonical_row:
                changed += 1
                if len(examples) < example_limit:
                    changed_columns = {
                        column: {"candidate": _display_value(candidate_value), "canonical": _display_value(canonical_value)}
                        for column, candidate_value, canonical_value in zip(columns, candidate_row, canonical_row)
                        if candidate_value != canonical_value
                    }
                    examples.append({"kind": "changed", "key": _key_example(primary_key, candidate_row, primary_key_indexes),
                                     "columns": changed_columns})
            candidate_row = _read_next(candidate_rows)
            canonical_row = _read_next(canonical_rows)

    message_parts = []
    if added:
        message_parts.append("%d added" % added)
    if removed:
        message_parts.append("%d removed" % removed)
    if changed:
        message_parts.append("%d changed" % changed)
    return ("SAME" if not message_parts else "DATA DIFFERENT", tuple(examples), ", ".join(message_parts))


def _compare_unkeyed_rows(candidate: sqlite3.Connection, canonical: sqlite3.Connection,
                          table_name: str, columns: Tuple[str, ...],
                          example_limit: int) -> Tuple[str, Tuple[Dict[str, Any], ...], str]:
    """Compare tables without a key as sorted multisets without loading them into memory."""
    order = ", ".join("%s COLLATE BINARY" % _quote_identifier(column) for column in columns)
    query = "SELECT * FROM %s ORDER BY %s" % (_quote_identifier(table_name), order)
    candidate_rows = iter(candidate.execute(query))
    canonical_rows = iter(canonical.execute(query))
    candidate_row = _read_next(candidate_rows)
    canonical_row = _read_next(canonical_rows)
    examples = []
    added = removed = 0
    all_indexes = tuple(range(len(columns)))

    while candidate_row is not None or canonical_row is not None:
        if canonical_row is None:
            added += 1
            if len(examples) < example_limit:
                examples.append({"kind": "added", "candidate": _display_row(columns, candidate_row)})
            candidate_row = _read_next(candidate_rows)
            continue
        if candidate_row is None:
            removed += 1
            if len(examples) < example_limit:
                examples.append({"kind": "removed", "canonical": _display_row(columns, canonical_row)})
            canonical_row = _read_next(canonical_rows)
            continue

        candidate_key = _row_key(candidate_row, all_indexes)
        canonical_key = _row_key(canonical_row, all_indexes)
        if candidate_key < canonical_key:
            added += 1
            if len(examples) < example_limit:
                examples.append({"kind": "added", "candidate": _display_row(columns, candidate_row)})
            candidate_row = _read_next(candidate_rows)
        elif candidate_key > canonical_key:
            removed += 1
            if len(examples) < example_limit:
                examples.append({"kind": "removed", "canonical": _display_row(columns, canonical_row)})
            canonical_row = _read_next(canonical_rows)
        else:
            candidate_row = _read_next(candidate_rows)
            canonical_row = _read_next(canonical_rows)

    message_parts = []
    if added:
        message_parts.append("%d added" % added)
    if removed:
        message_parts.append("%d removed" % removed)
    if not message_parts:
        return "SAME", (), "No primary key. Rows were compared as multisets."
    return "DATA DIFFERENT", tuple(examples), ", ".join(message_parts) + "; no primary key"

def _compare_identifier_normalized_rows(
    connection_a: sqlite3.Connection,
    connection_b: sqlite3.Connection,
    table_name: str,
    columns: Tuple[str, ...],
    primary_key: Tuple[str, ...],
    context_a: Dict[str, Any],
    context_b: Dict[str, Any],
    example_limit: int,
) -> Tuple[str, Tuple[Dict[str, Any], ...], str]:
    """Compare person-linked rows as semantic multisets after stable-ID normalization."""
    rows_a = [
        _canonicalize_row(
            table_name,
            columns,
            primary_key,
            row,
            context_a,
            normalize_volatile=False,
        )
        for row in connection_a.execute(
            "SELECT * FROM %s" % _quote_identifier(table_name)
        )
    ]

    rows_b = [
        _canonicalize_row(
            table_name,
            columns,
            primary_key,
            row,
            context_b,
            normalize_volatile=False,
        )
        for row in connection_b.execute(
            "SELECT * FROM %s" % _quote_identifier(table_name)
        )
    ]

    all_indexes = tuple(range(len(columns)))
    rows_a.sort(key=lambda row: _row_key(row, all_indexes))
    rows_b.sort(key=lambda row: _row_key(row, all_indexes))

    iterator_a = iter(rows_a)
    iterator_b = iter(rows_b)

    row_a = _read_next(iterator_a)
    row_b = _read_next(iterator_b)

    examples = []
    added = removed = 0

    while row_a is not None or row_b is not None:
        if row_b is None:
            added += 1
            if len(examples) < example_limit:
                examples.append(
                    {
                        "kind": "added",
                        "candidate": _display_row(columns, row_a),
                    }
                )
            row_a = _read_next(iterator_a)
            continue

        if row_a is None:
            removed += 1
            if len(examples) < example_limit:
                examples.append(
                    {
                        "kind": "removed",
                        "canonical": _display_row(columns, row_b),
                    }
                )
            row_b = _read_next(iterator_b)
            continue

        key_a = _row_key(row_a, all_indexes)
        key_b = _row_key(row_b, all_indexes)

        if key_a < key_b:
            added += 1
            if len(examples) < example_limit:
                examples.append(
                    {
                        "kind": "added",
                        "candidate": _display_row(columns, row_a),
                    }
                )
            row_a = _read_next(iterator_a)

        elif key_a > key_b:
            removed += 1
            if len(examples) < example_limit:
                examples.append(
                    {
                        "kind": "removed",
                        "canonical": _display_row(columns, row_b),
                    }
                )
            row_b = _read_next(iterator_b)

        else:
            row_a = _read_next(iterator_a)
            row_b = _read_next(iterator_b)

    if not added and not removed:
        return (
            "SAME",
            (),
            "Rows match after stable person-identifier normalization.",
        )

    parts = []

    if added:
        parts.append("%d added" % added)

    if removed:
        parts.append("%d removed" % removed)

    return (
        "DATA DIFFERENT",
        tuple(examples),
        ", ".join(parts) + "; stable person identifiers applied",
    )

def _schema_object_map(
    connection: sqlite3.Connection,
) -> Dict[Tuple[str, str, str], str]:
    rows = connection.execute(
        "SELECT type, name, tbl_name, COALESCE(sql, '') "
        "FROM sqlite_master "
        "WHERE type IN ('table', 'index', 'view', 'trigger') "
        "AND name NOT LIKE 'sqlite_%'"
    )

    return {
        (row[0], row[1], row[2]): row[3]
        for row in rows
    }


def _schema_change_descriptions(
    candidate: sqlite3.Connection,
    canonical: sqlite3.Connection,
) -> Tuple[str, ...]:
    candidate_schema = _schema_object_map(candidate)
    canonical_schema = _schema_object_map(canonical)

    changes = []

    for key in sorted(
        set(candidate_schema) | set(canonical_schema)
    ):
        if candidate_schema.get(key) == canonical_schema.get(key):
            continue

        object_type, name, _table_name = key
        changes.append(
            "%s:%s" % (
                object_type,
                name,
            )
        )

    return tuple(changes)


def _person_identifier_values(
    identity_context: Dict[str, Any],
) -> Tuple[str, ...]:
    prefix = "@person:"
    values = []

    for token in identity_context[
        "stable_ids"
    ].get(
        "person",
        {},
    ).values():
        if not token.startswith(prefix):
            raise ComparisonError(
                "Unexpected stable person identity token: %s"
                % token
            )

        values.append(
            token[len(prefix):]
        )

    return tuple(
        sorted(values)
    )


def _semantic_table_digest(
    connection: sqlite3.Connection,
    table_name: str,
    identity_context: Dict[str, Any],
) -> str:
    columns, primary_key = _columns_and_primary_key(
        connection,
        table_name,
    )

    row_digests = []

    for row in connection.execute(
        "SELECT * FROM %s"
        % _quote_identifier(
            table_name
        )
    ):
        canonical_row = _canonicalize_row(
            table_name,
            columns,
            primary_key,
            row,
            identity_context,
            normalize_volatile=True,
        )

        row_digests.append(
            _row_digest(
                table_name,
                columns,
                canonical_row,
            )
        )

    digest = hashlib.sha256()
    digest.update(
        b"sqlite-semantic-table-v1"
    )

    _update_digest_value(
        digest,
        table_name,
    )

    for row_digest in sorted(
        row_digests
    ):
        digest.update(
            row_digest
        )

    return digest.hexdigest()


def _entity_semantic_digests(
    connection: sqlite3.Connection,
    identity_context: Dict[str, Any],
) -> Dict[str, str]:
    """
    Build one semantic digest per stable person identifier.

    Every person-derived row contributes to the digest of the person reached
    through its declared foreign-key path. This distinguishes an added or
    removed person from changed data on a person present in both files.
    """
    derived_tables = set(
        identity_context[
            "derived_tables"
        ]
    )

    stable_ids = identity_context[
        "stable_ids"
    ]

    foreign_keys = identity_context[
        "foreign_keys"
    ]

    if "person" not in derived_tables:
        return {}

    prefix = "@person:"

    person_tokens = stable_ids.get(
        "person",
        {},
    )

    roots_by_table: Dict[
        str,
        Dict[
            Any,
            frozenset[str],
        ],
    ] = {
        "person": {
            raw_id: frozenset(
                (
                    token[
                        len(prefix):
                    ],
                )
            )
            for raw_id, token
            in person_tokens.items()
        }
    }

    row_hashes: Dict[
        str,
        list[bytes],
    ] = {
        token[len(prefix):]: []
        for token
        in person_tokens.values()
    }

    remaining = set(
        derived_tables
    )

    while remaining:
        progressed = False

        for table_name in sorted(
            tuple(remaining)
        ):
            if table_name == "person":
                dependencies = set()

            else:
                dependencies = {
                    target_table
                    for (
                        target_table,
                        _target_column,
                    )
                    in foreign_keys.get(
                        table_name,
                        {},
                    ).values()
                    if (
                        target_table
                        in derived_tables
                        and target_table
                        != table_name
                    )
                }

            if not dependencies.issubset(
                roots_by_table
            ):
                continue

            columns, primary_key = (
                _columns_and_primary_key(
                    connection,
                    table_name,
                )
            )

            column_indexes = {
                column: index
                for index, column
                in enumerate(columns)
            }

            table_roots: Dict[
                Any,
                frozenset[str],
            ] = {}

            for row in connection.execute(
                "SELECT * FROM %s"
                % _quote_identifier(
                    table_name
                )
            ):
                if table_name == "person":
                    raw_id = row[
                        column_indexes[
                            primary_key[0]
                        ]
                    ]

                    roots = roots_by_table[
                        "person"
                    ].get(
                        raw_id,
                        frozenset(),
                    )

                else:
                    roots_set = set()

                    for (
                        column_name,
                        (
                            target_table,
                            _target_column,
                        ),
                    ) in foreign_keys.get(
                        table_name,
                        {},
                    ).items():
                        if (
                            target_table
                            not in derived_tables
                            or target_table
                            == table_name
                        ):
                            continue

                        value = row[
                            column_indexes[
                                column_name
                            ]
                        ]

                        if value is None:
                            continue

                        roots_set.update(
                            roots_by_table.get(
                                target_table,
                                {},
                            ).get(
                                value,
                                frozenset(),
                            )
                        )

                    roots = frozenset(
                        roots_set
                    )

                canonical_row = (
                    _canonicalize_row(
                        table_name,
                        columns,
                        primary_key,
                        row,
                        identity_context,
                        normalize_volatile=True,
                    )
                )

                semantic_row = _row_digest(
                    table_name,
                    columns,
                    canonical_row,
                )

                for identifier in roots:
                    row_hashes.setdefault(
                        identifier,
                        [],
                    ).append(
                        semantic_row
                    )

                if len(primary_key) == 1:
                    raw_id = row[
                        column_indexes[
                            primary_key[0]
                        ]
                    ]

                    table_roots[
                        raw_id
                    ] = roots

            roots_by_table[
                table_name
            ] = table_roots

            remaining.remove(
                table_name
            )

            progressed = True

        if not progressed:
            raise ComparisonError(
                "Could not resolve person-root dependencies for: %s"
                % ", ".join(
                    sorted(
                        remaining
                    )
                )
            )

    entity_digests = {}

    for (
        identifier,
        hashes,
    ) in row_hashes.items():
        digest = hashlib.sha256()

        digest.update(
            b"sqlite-semantic-entity-v1"
        )

        _update_digest_value(
            digest,
            identifier,
        )

        for row_hash in sorted(
            hashes
        ):
            digest.update(
                row_hash
            )

        entity_digests[
            identifier
        ] = digest.hexdigest()

    return entity_digests


def summarize_sqlite_comparison(
    candidate: Path | str,
    canonical: Path | str,
) -> SQLiteComparisonSummary:
    """
    Return structured semantic evidence and a high-level comparison tier.
    """
    candidate_connection = (
        _open_read_only_database(
            candidate
        )
    )

    canonical_connection = (
        _open_read_only_database(
            canonical
        )
    )

    try:
        schema_changes = (
            _schema_change_descriptions(
                candidate_connection,
                canonical_connection,
            )
        )

        candidate_context = (
            _build_identity_context(
                candidate_connection
            )
        )

        canonical_context = (
            _build_identity_context(
                canonical_connection
            )
        )

        candidate_identifiers = set(
            _person_identifier_values(
                candidate_context
            )
        )

        canonical_identifiers = set(
            _person_identifier_values(
                canonical_context
            )
        )

        added_identifiers = tuple(
            sorted(
                candidate_identifiers
                - canonical_identifiers
            )
        )

        removed_identifiers = tuple(
            sorted(
                canonical_identifiers
                - candidate_identifiers
            )
        )

        matched_identifiers = (
            candidate_identifiers
            & canonical_identifiers
        )

        candidate_entity_digests = (
            _entity_semantic_digests(
                candidate_connection,
                candidate_context,
            )
        )

        canonical_entity_digests = (
            _entity_semantic_digests(
                canonical_connection,
                canonical_context,
            )
        )

        changed_identifiers = tuple(
            sorted(
                identifier
                for identifier
                in matched_identifiers
                if (
                    candidate_entity_digests.get(
                        identifier
                    )
                    != canonical_entity_digests.get(
                        identifier
                    )
                )
            )
        )

        candidate_tables = set(
            _user_table_names(
                candidate_connection
            )
        )

        canonical_tables = set(
            _user_table_names(
                canonical_connection
            )
        )

        changed_tables = []

        for table_name in sorted(
            candidate_tables
            & canonical_tables
        ):
            if (
                _schema_signature(
                    candidate_connection,
                    table_name,
                )
                != _schema_signature(
                    canonical_connection,
                    table_name,
                )
            ):
                continue

            (
                candidate_columns,
                candidate_primary_key,
            ) = _columns_and_primary_key(
                candidate_connection,
                table_name,
            )

            (
                canonical_columns,
                canonical_primary_key,
            ) = _columns_and_primary_key(
                canonical_connection,
                table_name,
            )

            if (
                candidate_columns
                != canonical_columns
                or candidate_primary_key
                != canonical_primary_key
            ):
                continue

            if (
                _semantic_table_digest(
                    candidate_connection,
                    table_name,
                    candidate_context,
                )
                != _semantic_table_digest(
                    canonical_connection,
                    table_name,
                    canonical_context,
                )
            ):
                changed_tables.append(
                    table_name
                )

        changed_tables_tuple = tuple(
            changed_tables
        )

        derived_tables = (
            set(
                candidate_context[
                    "derived_tables"
                ]
            )
            | set(
                canonical_context[
                    "derived_tables"
                ]
            )
        )

        non_entity_data_changed = any(
            table_name
            not in derived_tables
            for table_name
            in changed_tables_tuple
        )

        if schema_changes:
            tier = "STRUCTURE CHANGED"

        elif (
            added_identifiers
            or removed_identifiers
        ):
            if (
                changed_identifiers
                or non_entity_data_changed
            ):
                tier = (
                    "ENTITIES + DATA CHANGED"
                )

            else:
                tier = "ENTITIES CHANGED"

        elif changed_tables_tuple:
            tier = "DATA CHANGED"

        else:
            tier = "SAME"

        return SQLiteComparisonSummary(
            tier=tier,
            candidate_entity_count=len(
                candidate_identifiers
            ),
            canonical_entity_count=len(
                canonical_identifiers
            ),
            matched_entity_count=len(
                matched_identifiers
            ),
            added_identifiers=(
                added_identifiers
            ),
            removed_identifiers=(
                removed_identifiers
            ),
            changed_entity_count=len(
                changed_identifiers
            ),
            changed_identifiers=(
                changed_identifiers
            ),
            changed_tables=(
                changed_tables_tuple
            ),
            schema_changes=(
                schema_changes
            ),
        )

    except sqlite3.Error as exc:
        raise ComparisonError(
            "Could not summarize SQLite comparison: %s"
            % exc
        ) from exc

    finally:
        candidate_connection.close()
        canonical_connection.close()

def inspect_sqlite_differences(candidate: Path | str, canonical: Path | str,
                               *, example_limit: int = DIFF_EXAMPLE_LIMIT) -> SQLiteDifferenceReport:
    """Inspect SQLite schema and keyed row differences without modifying either database."""
    if example_limit < 1:
        raise ComparisonError("Example limit must be at least 1.")
    candidate_path = Path(candidate)
    canonical_path = Path(canonical)
    candidate_connection = _open_read_only_database(candidate_path)
    canonical_connection = _open_read_only_database(canonical_path)
    try:
        candidate_tables = set(_user_table_names(candidate_connection))
        canonical_tables = set(_user_table_names(canonical_connection))
        candidate_identity_context = _build_identity_context(candidate_connection)
        canonical_identity_context = _build_identity_context(canonical_connection)
        differences = []
        for table_name in sorted(candidate_tables | canonical_tables):
            in_candidate = table_name in candidate_tables
            in_canonical = table_name in canonical_tables
            if not in_canonical:
                differences.append(TableDifference(
                    table_name, "TABLE ADDED", _row_count(candidate_connection, table_name), None,
                    message="Table exists only in the candidate database.",
                ))
                continue
            if not in_candidate:
                differences.append(TableDifference(
                    table_name, "TABLE REMOVED", None, _row_count(canonical_connection, table_name),
                    message="Table exists only in the canonical database.",
                ))
                continue

            candidate_rows = _row_count(candidate_connection, table_name)
            canonical_rows = _row_count(canonical_connection, table_name)
            candidate_schema = _schema_signature(candidate_connection, table_name)
            canonical_schema = _schema_signature(canonical_connection, table_name)
            candidate_columns, candidate_primary_key = _columns_and_primary_key(candidate_connection, table_name)
            canonical_columns, canonical_primary_key = _columns_and_primary_key(canonical_connection, table_name)
            if candidate_schema != canonical_schema:
                differences.append(TableDifference(
                    table_name, "SCHEMA DIFFERENT", candidate_rows, canonical_rows,
                    candidate_primary_key,
                    "Table, index, or trigger definitions differ. Row comparison was not run.",
                ))
                continue
            if candidate_primary_key != canonical_primary_key:
                differences.append(TableDifference(
                    table_name, "SCHEMA DIFFERENT", candidate_rows, canonical_rows, candidate_primary_key,
                    "Primary key definitions differ. Row comparison was not run.",
                ))
                continue
            if candidate_columns != canonical_columns:
                differences.append(TableDifference(
                    table_name, "SCHEMA DIFFERENT", candidate_rows, canonical_rows, candidate_primary_key,
                    "Column definitions differ. Row comparison was not run.",
                ))
                continue
            identifier_normalized = (
                    table_name in candidate_identity_context["derived_tables"]
                    or table_name in canonical_identity_context["derived_tables"]
                    or table_name in candidate_identity_context["stable_ids"]
                    or table_name in canonical_identity_context["stable_ids"]
            )
            if identifier_normalized:
                state, examples, message = _compare_identifier_normalized_rows(
                    candidate_connection, canonical_connection, table_name, candidate_columns,
                    candidate_primary_key, candidate_identity_context, canonical_identity_context, example_limit,
                )
            elif candidate_primary_key:
                state, examples, message = _compare_keyed_rows(
                    candidate_connection, canonical_connection, table_name, candidate_columns,
                    candidate_primary_key, example_limit,
                )
            else:
                state, examples, message = _compare_unkeyed_rows(
                    candidate_connection, canonical_connection, table_name, candidate_columns, example_limit,
                )
            differences.append(TableDifference(
                table_name, state, candidate_rows, canonical_rows, candidate_primary_key, message, examples,
            ))
        return SQLiteDifferenceReport(candidate_path, canonical_path, tuple(differences))
    except sqlite3.Error as exc:
        raise ComparisonError("Could not inspect SQLite differences: %s" % exc) from exc
    finally:
        candidate_connection.close()
        canonical_connection.close()

def format_comparison_summary(
    summary: SQLiteComparisonSummary,
) -> str:
    """
    Format semantic comparison evidence for concise GUI display.

    This is deliberately informational only. Workflow permissions continue
    to use the existing SAME / DIFFERENT comparison state for now.
    """
    lines = [
        "Comparison tier: %s"
        % summary.tier,
        (
            "Entities: %d candidate | %d canonical | %d matched"
            % (
                summary.candidate_entity_count,
                summary.canonical_entity_count,
                summary.matched_entity_count,
            )
        ),
    ]

    added_count = len(
        summary.added_identifiers
    )

    removed_count = len(
        summary.removed_identifiers
    )

    if (
        added_count
        or removed_count
        or summary.changed_entity_count
    ):
        lines.append(
            (
                "Entity changes: +%d added | -%d removed | %d matched changed"
                % (
                    added_count,
                    removed_count,
                    summary.changed_entity_count,
                )
            )
        )

    if summary.changed_tables:
        lines.append(
            "Changed tables: %s"
            % ", ".join(
                summary.changed_tables
            )
        )

    if summary.schema_changes:
        lines.append(
            "Structure changes: %s"
            % ", ".join(
                summary.schema_changes
            )
        )

    return "\n".join(
        lines
    )



def compare_feature(feature_number: str, script_runs_dir: Path | str,
                    canonical_dir: Path | str | None = None) -> ComparisonResult:
    feature = _feature(feature_number)
    root = Path(script_runs_dir).expanduser()
    canonical_root = Path(canonical_dir).expanduser() if canonical_dir is not None else root.parent / "Canonical Outputs"
    candidate = root / ("feature_" + feature) / (feature + ".sqlite")
    canonical = canonical_root / (feature + ".sqlite")
    try:
        if not candidate.is_file():
            return ComparisonResult(feature, "NO CANDIDATE", candidate, canonical)
        candidate_hash = sha256_file(candidate)
        if not canonical.is_file():
            return ComparisonResult(feature, "NO CANONICAL", candidate, canonical, candidate_hash)
        canonical_hash = sha256_file(canonical)
        byte_identical = (
            candidate.stat().st_size == canonical.stat().st_size
            and candidate_hash == canonical_hash
        )
        candidate_logical_hash = logical_sha256_file(candidate)
        canonical_logical_hash = logical_sha256_file(canonical)

        comparison_summary = summarize_sqlite_comparison(
            candidate,
            canonical,
        )

        logical_same = candidate_logical_hash == canonical_logical_hash

        summary_message = format_comparison_summary(
            comparison_summary
        )

        if logical_same:
            base_message = (
                "Logical contents match; files are byte-identical."
                if byte_identical else
                "Logical contents match after stable person-identifier normalization and "
                "the documented volatile-field normalization; file bytes differ."
            )

            message = (
                    base_message
                    + "\n\n"
                    + summary_message
            )

            return ComparisonResult(
                feature,
                "SAME",
                candidate,
                canonical,
                candidate_hash,
                canonical_hash,
                message,
                candidate_logical_hash,
                canonical_logical_hash,
                byte_identical,
                comparison_summary,
            )

        base_message = (
                "Logical contents differ; file bytes %s."
                % (
                    "match"
                    if byte_identical
                    else "differ"
                )
        )

        message = (
                base_message
                + "\n\n"
                + summary_message
        )

        return ComparisonResult(
            feature,
            "DIFFERENT",
            candidate,
            canonical,
            candidate_hash,
            canonical_hash,
            message,
            candidate_logical_hash,
            canonical_logical_hash,
            byte_identical,
            comparison_summary,
        )
    except Exception as exc:
        return ComparisonResult(feature, "ERROR", candidate, canonical, message=str(exc))


def compare_features(features, script_runs_dir, canonical_dir=None):
    return [compare_feature(feature, script_runs_dir, canonical_dir) for feature in features]

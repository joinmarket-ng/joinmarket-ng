"""
Tests for UTXO metadata persistence (BIP-329 JSONL format).

Tests cover:
- OutputRecord serialization/deserialization
- UTXOMetadataStore load/save with atomic writes
- Freeze/unfreeze/toggle operations
- Label management
- Edge cases: missing files, malformed lines, cleanup of empty records
- BIP-329 format compliance
"""

from __future__ import annotations

import json

import pytest

from jmwallet.wallet.utxo_metadata import (
    USED_LABEL_PREFIX,
    AddressRecord,
    OutputRecord,
    ReservedAddressRecord,
    UTXOMetadataStore,
    load_metadata_store,
)

# ---------------------------------------------------------------------------
# OutputRecord tests
# ---------------------------------------------------------------------------


class TestOutputRecord:
    """Tests for BIP-329 OutputRecord dataclass."""

    def test_basic_creation(self):
        """Create a record with just a ref."""
        r = OutputRecord(ref="aabb:0")
        assert r.ref == "aabb:0"
        assert r.spendable is None
        assert r.label is None
        assert r.is_frozen is False

    def test_frozen_record(self):
        """spendable=False means frozen."""
        r = OutputRecord(ref="aabb:0", spendable=False)
        assert r.is_frozen is True

    def test_spendable_record(self):
        """spendable=True means not frozen."""
        r = OutputRecord(ref="aabb:0", spendable=True)
        assert r.is_frozen is False

    def test_to_dict_minimal(self):
        """to_dict with only ref omits optional fields."""
        r = OutputRecord(ref="aabb:0")
        d = r.to_dict()
        assert d == {"type": "output", "ref": "aabb:0"}
        assert "spendable" not in d
        assert "label" not in d

    def test_to_dict_frozen(self):
        """to_dict includes spendable when set."""
        r = OutputRecord(ref="aabb:0", spendable=False)
        d = r.to_dict()
        assert d == {"type": "output", "ref": "aabb:0", "spendable": False}

    def test_to_dict_with_label(self):
        """to_dict includes label when set."""
        r = OutputRecord(ref="aabb:0", label="cold storage")
        d = r.to_dict()
        assert d == {"type": "output", "ref": "aabb:0", "label": "cold storage"}

    def test_to_dict_full(self):
        """to_dict with all fields."""
        r = OutputRecord(ref="aabb:0", spendable=False, label="frozen funds")
        d = r.to_dict()
        assert d == {
            "type": "output",
            "ref": "aabb:0",
            "spendable": False,
            "label": "frozen funds",
        }

    def test_from_dict_valid(self):
        """from_dict with valid output record."""
        d = {"type": "output", "ref": "aabb:0", "spendable": False, "label": "test"}
        r = OutputRecord.from_dict(d)
        assert r is not None
        assert r.ref == "aabb:0"
        assert r.spendable is False
        assert r.label == "test"

    def test_from_dict_minimal(self):
        """from_dict with only required fields."""
        d = {"type": "output", "ref": "aabb:0"}
        r = OutputRecord.from_dict(d)
        assert r is not None
        assert r.ref == "aabb:0"
        assert r.spendable is None
        assert r.label is None

    def test_from_dict_wrong_type(self):
        """from_dict returns None for non-output type."""
        d = {"type": "tx", "ref": "aabb"}
        assert OutputRecord.from_dict(d) is None

    def test_from_dict_missing_type(self):
        """from_dict returns None when type is missing."""
        d = {"ref": "aabb:0"}
        assert OutputRecord.from_dict(d) is None

    def test_from_dict_missing_ref(self):
        """from_dict returns None when ref is missing."""
        d = {"type": "output"}
        assert OutputRecord.from_dict(d) is None

    def test_from_dict_invalid_ref_type(self):
        """from_dict returns None when ref is not a string."""
        d = {"type": "output", "ref": 123}
        assert OutputRecord.from_dict(d) is None

    def test_from_dict_invalid_spendable_type(self):
        """from_dict returns None when spendable is not a bool."""
        d = {"type": "output", "ref": "aabb:0", "spendable": "yes"}
        assert OutputRecord.from_dict(d) is None

    def test_from_dict_coerces_label_to_str(self):
        """from_dict coerces non-string label to string."""
        d = {"type": "output", "ref": "aabb:0", "label": 42}
        r = OutputRecord.from_dict(d)
        assert r is not None
        assert r.label == "42"

    def test_roundtrip(self):
        """to_dict -> from_dict roundtrip preserves data."""
        original = OutputRecord(ref="aa" * 32 + ":1", spendable=False, label="test label")
        d = original.to_dict()
        restored = OutputRecord.from_dict(d)
        assert restored is not None
        assert restored.ref == original.ref
        assert restored.spendable == original.spendable
        assert restored.label == original.label


# ---------------------------------------------------------------------------
# UTXOMetadataStore tests
# ---------------------------------------------------------------------------


class TestUTXOMetadataStore:
    """Tests for UTXOMetadataStore persistence and operations."""

    @pytest.fixture
    def store_path(self, tmp_path):
        """Return a path for the metadata file in a temp directory."""
        return tmp_path / "wallet_metadata.jsonl"

    @pytest.fixture
    def store(self, store_path):
        """Create a fresh UTXOMetadataStore."""
        return UTXOMetadataStore(path=store_path)

    @pytest.fixture
    def outpoint_a(self):
        return "aa" * 32 + ":0"

    @pytest.fixture
    def outpoint_b(self):
        return "bb" * 32 + ":1"

    @pytest.fixture
    def outpoint_c(self):
        return "cc" * 32 + ":2"

    # --- Load/Save ---

    def test_load_missing_file(self, store):
        """Loading with no file on disk results in empty store."""
        store.load()
        assert len(store.records) == 0

    def test_save_and_load_roundtrip(self, store, outpoint_a):
        """Save records and load them back."""
        store.freeze(outpoint_a)
        # Create a new store and load from same path
        store2 = UTXOMetadataStore(path=store.path)
        store2.load()
        assert store2.is_frozen(outpoint_a)

    def test_save_creates_parent_dirs(self, tmp_path, outpoint_a):
        """Save creates parent directories if they don't exist."""
        deep_path = tmp_path / "a" / "b" / "c" / "metadata.jsonl"
        store = UTXOMetadataStore(path=deep_path)
        store.freeze(outpoint_a)
        assert deep_path.exists()

    def test_save_removes_file_when_empty(self, store, outpoint_a):
        """Save removes the file when no meaningful records remain."""
        store.freeze(outpoint_a)
        assert store.path.exists()
        store.unfreeze(outpoint_a)
        assert not store.path.exists()

    def test_load_skips_malformed_lines(self, store_path):
        """Malformed JSON lines are skipped during load."""
        store_path.write_text(
            '{"type":"output","ref":"aa:0","spendable":false}\n'
            "not valid json\n"
            '{"type":"output","ref":"bb:1","spendable":false}\n',
            encoding="utf-8",
        )
        store = UTXOMetadataStore(path=store_path)
        store.load()
        assert len(store.records) == 2
        assert store.is_frozen("aa:0")
        assert store.is_frozen("bb:1")

    def test_load_skips_non_output_records(self, store_path):
        """Non-output BIP-329 records are ignored (per spec)."""
        store_path.write_text(
            '{"type":"tx","ref":"aabb","label":"payment"}\n'
            '{"type":"output","ref":"cc:0","spendable":false}\n',
            encoding="utf-8",
        )
        store = UTXOMetadataStore(path=store_path)
        store.load()
        assert len(store.records) == 1
        assert store.is_frozen("cc:0")

    def test_load_skips_empty_lines(self, store_path):
        """Empty lines are gracefully skipped."""
        store_path.write_text(
            '\n\n{"type":"output","ref":"aa:0","spendable":false}\n\n',
            encoding="utf-8",
        )
        store = UTXOMetadataStore(path=store_path)
        store.load()
        assert len(store.records) == 1

    def test_load_last_wins_for_duplicate_outpoints(self, store_path):
        """When the same outpoint appears twice, the last record wins."""
        store_path.write_text(
            '{"type":"output","ref":"aa:0","spendable":false}\n'
            '{"type":"output","ref":"aa:0","spendable":true}\n',
            encoding="utf-8",
        )
        store = UTXOMetadataStore(path=store_path)
        store.load()
        assert not store.is_frozen("aa:0")

    def test_save_deterministic_order(self, store, outpoint_a, outpoint_b, outpoint_c):
        """Records are saved sorted by ref for deterministic output."""
        store.freeze(outpoint_c)
        store.freeze(outpoint_a)
        store.freeze(outpoint_b)

        text = store.path.read_text(encoding="utf-8")
        lines = [line for line in text.strip().split("\n") if line]
        refs = [json.loads(line)["ref"] for line in lines]
        assert refs == sorted(refs)

    def test_save_compact_json(self, store, outpoint_a):
        """Saved JSON uses compact separators (no spaces)."""
        store.freeze(outpoint_a)
        text = store.path.read_text(encoding="utf-8").strip()
        # Should not contain ": " or ", " patterns (compact separators)
        assert ": " not in text
        assert ", " not in text

    # --- Freeze / Unfreeze ---

    def test_freeze(self, store, outpoint_a):
        """Freezing an outpoint sets spendable=False."""
        store.freeze(outpoint_a)
        assert store.is_frozen(outpoint_a)

    def test_freeze_persists_immediately(self, store, outpoint_a):
        """Each freeze call writes to disk."""
        store.freeze(outpoint_a)
        assert store.path.exists()
        # Verify by loading a new store
        store2 = UTXOMetadataStore(path=store.path)
        store2.load()
        assert store2.is_frozen(outpoint_a)

    def test_freeze_already_frozen(self, store, outpoint_a):
        """Freezing an already frozen outpoint is a no-op (still frozen)."""
        store.freeze(outpoint_a)
        store.freeze(outpoint_a)
        assert store.is_frozen(outpoint_a)

    def test_unfreeze(self, store, outpoint_a):
        """Unfreezing a frozen outpoint removes the record (when no label)."""
        store.freeze(outpoint_a)
        assert store.is_frozen(outpoint_a)
        store.unfreeze(outpoint_a)
        assert not store.is_frozen(outpoint_a)
        # Record should be removed entirely (no label)
        assert outpoint_a not in store.records

    def test_unfreeze_with_label_keeps_record(self, store, outpoint_a):
        """Unfreezing preserves the record if it has a label."""
        store.records[outpoint_a] = OutputRecord(ref=outpoint_a, spendable=False, label="important")
        store.save()
        store.unfreeze(outpoint_a)
        assert not store.is_frozen(outpoint_a)
        # Record still exists for the label
        assert outpoint_a in store.records
        assert store.records[outpoint_a].label == "important"
        assert store.records[outpoint_a].spendable is True

    def test_unfreeze_not_frozen(self, store, outpoint_a):
        """Unfreezing an outpoint that was never frozen is a no-op."""
        store.unfreeze(outpoint_a)
        assert not store.is_frozen(outpoint_a)
        assert outpoint_a not in store.records

    # --- Toggle ---

    def test_toggle_freezes_unfrozen(self, store, outpoint_a):
        """Toggle on unfrozen outpoint freezes it."""
        result = store.toggle_freeze(outpoint_a)
        assert result is True
        assert store.is_frozen(outpoint_a)

    def test_toggle_unfreezes_frozen(self, store, outpoint_a):
        """Toggle on frozen outpoint unfreezes it."""
        store.freeze(outpoint_a)
        result = store.toggle_freeze(outpoint_a)
        assert result is False
        assert not store.is_frozen(outpoint_a)

    def test_toggle_roundtrip(self, store, outpoint_a):
        """Double toggle returns to original state."""
        store.toggle_freeze(outpoint_a)  # freeze
        store.toggle_freeze(outpoint_a)  # unfreeze
        assert not store.is_frozen(outpoint_a)

    # --- get_frozen_outpoints ---

    def test_get_frozen_outpoints_empty(self, store):
        """Empty store returns empty set."""
        assert store.get_frozen_outpoints() == set()

    def test_get_frozen_outpoints(self, store, outpoint_a, outpoint_b, outpoint_c):
        """Returns only frozen outpoints."""
        store.freeze(outpoint_a)
        store.freeze(outpoint_b)
        store.set_label(outpoint_c, "just a label")  # Not frozen, just labeled

        frozen = store.get_frozen_outpoints()
        assert frozen == {outpoint_a, outpoint_b}

    # --- Labels ---

    def test_set_label(self, store, outpoint_a):
        """Setting a label creates a record."""
        store.set_label(outpoint_a, "my label")
        assert store.get_label(outpoint_a) == "my label"

    def test_clear_label_removes_record_if_no_other_metadata(self, store, outpoint_a):
        """Clearing label removes the record if no freeze state is set."""
        store.set_label(outpoint_a, "test")
        store.set_label(outpoint_a, None)
        assert outpoint_a not in store.records

    def test_clear_label_keeps_frozen_state(self, store, outpoint_a):
        """Clearing label preserves frozen state."""
        store.records[outpoint_a] = OutputRecord(ref=outpoint_a, spendable=False, label="test")
        store.save()
        store.set_label(outpoint_a, None)
        # Record should still exist for the freeze
        assert outpoint_a in store.records
        assert store.is_frozen(outpoint_a)
        assert store.get_label(outpoint_a) is None

    def test_get_label_nonexistent(self, store, outpoint_a):
        """get_label returns None for unknown outpoints."""
        assert store.get_label(outpoint_a) is None

    # --- is_frozen ---

    def test_is_frozen_unknown_outpoint(self, store):
        """is_frozen returns False for unknown outpoints."""
        assert not store.is_frozen("nonexistent:0")

    def test_is_frozen_spendable_true(self, store, outpoint_a):
        """is_frozen returns False when spendable=True."""
        store.records[outpoint_a] = OutputRecord(ref=outpoint_a, spendable=True)
        assert not store.is_frozen(outpoint_a)

    def test_is_frozen_spendable_none(self, store, outpoint_a):
        """is_frozen returns False when spendable=None."""
        store.records[outpoint_a] = OutputRecord(ref=outpoint_a, spendable=None)
        assert not store.is_frozen(outpoint_a)


# ---------------------------------------------------------------------------
# BIP-329 format compliance
# ---------------------------------------------------------------------------


class TestBIP329Compliance:
    """Tests verifying BIP-329 format compliance."""

    def test_output_type_field(self):
        """Records always have type='output'."""
        r = OutputRecord(ref="aabb:0", spendable=False)
        assert r.to_dict()["type"] == "output"

    def test_ref_is_txid_colon_vout(self, tmp_path):
        """Outpoints follow txid:vout format."""
        store = UTXOMetadataStore(path=tmp_path / "meta.jsonl")
        outpoint = "ab" * 32 + ":42"
        store.freeze(outpoint)
        text = store.path.read_text(encoding="utf-8").strip()
        record = json.loads(text)
        assert record["ref"] == outpoint
        assert ":" in record["ref"]

    def test_spendable_false_means_frozen(self):
        """BIP-329 spendable=false maps to frozen."""
        d = {"type": "output", "ref": "aa:0", "spendable": False}
        r = OutputRecord.from_dict(d)
        assert r is not None
        assert r.is_frozen is True

    def test_spendable_absent_means_no_opinion(self):
        """BIP-329 absent spendable means wallet should not alter state."""
        d = {"type": "output", "ref": "aa:0", "label": "test"}
        r = OutputRecord.from_dict(d)
        assert r is not None
        assert r.spendable is None
        assert r.is_frozen is False

    def test_jsonl_format_one_record_per_line(self, tmp_path):
        """Each record is on its own line (JSONL format)."""
        store = UTXOMetadataStore(path=tmp_path / "meta.jsonl")
        store.freeze("aa:0")
        store.freeze("bb:1")
        text = store.path.read_text(encoding="utf-8")
        lines = [line for line in text.strip().split("\n") if line]
        assert len(lines) == 2
        for line in lines:
            record = json.loads(line)
            assert record["type"] == "output"

    def test_interop_with_sparrow_format(self, tmp_path):
        """Verify format is compatible with Sparrow wallet's BIP-329 export."""
        # Sparrow exports labels like:
        # {"type":"output","ref":"txid:vout","label":"Label Text","spendable":false}
        sparrow_line = (
            '{"type":"output","ref":"' + "ab" * 32 + ':0","label":"My UTXO","spendable":false}'
        )
        path = tmp_path / "sparrow_export.jsonl"
        path.write_text(sparrow_line + "\n", encoding="utf-8")

        store = UTXOMetadataStore(path=path)
        store.load()

        outpoint = "ab" * 32 + ":0"
        assert store.is_frozen(outpoint)
        assert store.get_label(outpoint) == "My UTXO"


# ---------------------------------------------------------------------------
# Error handling and writability tests
# ---------------------------------------------------------------------------


class TestSaveErrorPropagation:
    """Tests that save() failures propagate to callers."""

    @pytest.fixture
    def readonly_store(self, tmp_path):
        """Create a store in a directory that will be made read-only."""
        path = tmp_path / "metadata.jsonl"
        store = UTXOMetadataStore(path=path)
        return store

    @pytest.fixture
    def outpoint(self):
        return "aa" * 32 + ":0"

    def test_save_raises_on_readonly_directory(self, tmp_path, outpoint):
        """save() raises OSError when directory is read-only."""
        path = tmp_path / "metadata.jsonl"
        store = UTXOMetadataStore(path=path)
        # Make directory read-only
        tmp_path.chmod(0o555)
        try:
            with pytest.raises(OSError):
                store.freeze(outpoint)
        finally:
            # Restore permissions for cleanup
            tmp_path.chmod(0o755)

    def test_freeze_propagates_save_error(self, tmp_path, outpoint):
        """freeze() propagates OSError from save()."""
        path = tmp_path / "metadata.jsonl"
        store = UTXOMetadataStore(path=path)
        tmp_path.chmod(0o555)
        try:
            with pytest.raises(OSError):
                store.freeze(outpoint)
            # In-memory state may have changed, but disk is unchanged
        finally:
            tmp_path.chmod(0o755)

    def test_unfreeze_propagates_save_error(self, tmp_path, outpoint):
        """unfreeze() propagates OSError from save()."""
        path = tmp_path / "metadata.jsonl"
        store = UTXOMetadataStore(path=path)
        # First, freeze successfully
        store.freeze(outpoint)
        assert store.is_frozen(outpoint)
        # Now make read-only
        tmp_path.chmod(0o555)
        try:
            with pytest.raises(OSError):
                store.unfreeze(outpoint)
        finally:
            tmp_path.chmod(0o755)

    def test_toggle_freeze_propagates_save_error(self, tmp_path, outpoint):
        """toggle_freeze() propagates OSError from save()."""
        path = tmp_path / "metadata.jsonl"
        store = UTXOMetadataStore(path=path)
        tmp_path.chmod(0o555)
        try:
            with pytest.raises(OSError):
                store.toggle_freeze(outpoint)
        finally:
            tmp_path.chmod(0o755)

    def test_set_label_propagates_save_error(self, tmp_path, outpoint):
        """set_label() propagates OSError from save()."""
        path = tmp_path / "metadata.jsonl"
        store = UTXOMetadataStore(path=path)
        tmp_path.chmod(0o555)
        try:
            with pytest.raises(OSError):
                store.set_label(outpoint, "test label")
        finally:
            tmp_path.chmod(0o755)


class TestVerifyWritable:
    """Tests for verify_writable() method."""

    def test_writable_directory_passes(self, tmp_path):
        """verify_writable() succeeds on a writable directory."""
        store = UTXOMetadataStore(path=tmp_path / "metadata.jsonl")
        store.verify_writable()  # Should not raise

    def test_readonly_directory_raises(self, tmp_path):
        """verify_writable() raises OSError on read-only directory."""
        store = UTXOMetadataStore(path=tmp_path / "metadata.jsonl")
        tmp_path.chmod(0o555)
        try:
            with pytest.raises(OSError, match="not writable"):
                store.verify_writable()
        finally:
            tmp_path.chmod(0o755)

    def test_creates_parent_dirs(self, tmp_path):
        """verify_writable() creates parent directories if needed."""
        deep_path = tmp_path / "a" / "b" / "metadata.jsonl"
        store = UTXOMetadataStore(path=deep_path)
        store.verify_writable()
        assert deep_path.parent.exists()

    def test_nonexistent_parent_readonly(self, tmp_path):
        """verify_writable() raises when parent can't be created."""
        # Make tmp_path read-only so mkdir fails
        tmp_path.chmod(0o555)
        deep_path = tmp_path / "newdir" / "metadata.jsonl"
        store = UTXOMetadataStore(path=deep_path)
        try:
            with pytest.raises(OSError):
                store.verify_writable()
        finally:
            tmp_path.chmod(0o755)


# ---------------------------------------------------------------------------
# Address history tests (BIP-329 ``addr`` records with ``jm:used`` label)
# ---------------------------------------------------------------------------


class TestAddressRecord:
    """Tests for the AddressRecord dataclass."""

    def test_default_label_recognized_as_used(self):
        rec = AddressRecord(ref="bcrt1qabc")
        assert rec.label.startswith(USED_LABEL_PREFIX)
        assert rec.origins == set()

    def test_label_with_origin_round_trip(self):
        rec = AddressRecord(ref="bcrt1qabc", label="jm:used:deposit")
        assert rec.origins == {"deposit"}
        data = rec.to_dict()
        assert data == {"type": "addr", "ref": "bcrt1qabc", "label": "jm:used:deposit"}
        rec2 = AddressRecord.from_dict(data)
        assert rec2 == rec

    def test_with_added_origin_accumulates_sorted(self):
        rec = AddressRecord(ref="bcrt1qabc", label="jm:used:deposit")
        rec = rec.with_added_origin("cj_in")
        rec = rec.with_added_origin("cj_in")  # idempotent
        assert rec.origins == {"cj_in", "deposit"}
        # Sorted, comma-joined for determinism on disk.
        assert rec.label == "jm:used:cj_in,deposit"

    def test_from_dict_rejects_non_jm_used_label(self):
        # Foreign Sparrow-style labels must not be picked up as jm:used.
        rec = AddressRecord.from_dict({"type": "addr", "ref": "bcrt1q", "label": "Donations"})
        assert rec is None

    def test_from_dict_rejects_wrong_type(self):
        rec = AddressRecord.from_dict({"type": "output", "ref": "aa:0"})
        assert rec is None


class TestReservedAddressRecord:
    """Tests for the ReservedAddressRecord dataclass."""

    def test_no_label_serializes_to_bare_prefix(self):
        rec = ReservedAddressRecord(ref="bcrt1qa")
        assert rec.label == "jm:reserved"
        assert rec.to_dict() == {"type": "addr", "ref": "bcrt1qa", "label": "jm:reserved"}

    def test_label_round_trip(self):
        rec = ReservedAddressRecord(ref="bcrt1qa", user_label="Alice")
        data = rec.to_dict()
        assert data == {"type": "addr", "ref": "bcrt1qa", "label": "jm:reserved:Alice"}
        assert ReservedAddressRecord.from_dict(data) == rec

    def test_from_dict_preserves_colons_in_label(self):
        rec = ReservedAddressRecord.from_dict(
            {"type": "addr", "ref": "bcrt1qa", "label": "jm:reserved:rent: March"}
        )
        assert rec is not None
        assert rec.user_label == "rent: March"

    def test_from_dict_rejects_non_reserved_label(self):
        assert (
            ReservedAddressRecord.from_dict(
                {"type": "addr", "ref": "bcrt1qa", "label": "jm:used:deposit"}
            )
            is None
        )


class TestMarkAddressUsed:
    """Tests for mark_address_used / mark_addresses_used / get_used_addresses."""

    def test_mark_and_persist(self, tmp_path):
        path = tmp_path / "m.jsonl"
        s = UTXOMetadataStore(path=path)
        s.load()
        assert s.mark_address_used("bcrt1qa", "deposit") is True
        # Idempotent: re-marking with same origin returns False (no disk write).
        assert s.mark_address_used("bcrt1qa", "deposit") is False
        # Adding a new origin extends the label and triggers a save.
        assert s.mark_address_used("bcrt1qa", "cj_in") is True

        s2 = UTXOMetadataStore(path=path)
        s2.load()
        assert s2.get_used_addresses() == {"bcrt1qa"}
        assert s2.address_records["bcrt1qa"].origins == {"cj_in", "deposit"}
        assert s2.is_address_used("bcrt1qa")
        assert not s2.is_address_used("bcrt1qother")

    def test_mark_many_batches_save(self, tmp_path):
        path = tmp_path / "m.jsonl"
        s = UTXOMetadataStore(path=path)
        s.load()
        changed = s.mark_addresses_used(["bcrt1qa", "bcrt1qb", "bcrt1qa"], "deposit")
        assert changed == 2
        s2 = UTXOMetadataStore(path=path)
        s2.load()
        assert s2.get_used_addresses() == {"bcrt1qa", "bcrt1qb"}

    def test_empty_address_ignored(self, tmp_path):
        s = UTXOMetadataStore(path=tmp_path / "m.jsonl")
        s.load()
        assert s.mark_address_used("", "deposit") is False
        assert s.get_used_addresses() == set()

    def test_coexists_with_output_records(self, tmp_path):
        path = tmp_path / "m.jsonl"
        s = UTXOMetadataStore(path=path)
        s.load()
        s.freeze("aa:0")
        s.mark_address_used("bcrt1qa", "deposit")
        s.set_label("aa:0", "spendme")

        s2 = UTXOMetadataStore(path=path)
        s2.load()
        assert s2.is_frozen("aa:0")
        assert s2.records["aa:0"].label == "spendme"
        assert s2.get_used_addresses() == {"bcrt1qa"}

    def test_preserves_foreign_addr_records(self, tmp_path):
        """Sparrow-style address-book labels survive a load/save round-trip."""
        path = tmp_path / "m.jsonl"
        foreign = {"type": "addr", "ref": "bcrt1qsparrow", "label": "Donations"}
        path.write_text(json.dumps(foreign) + "\n", encoding="utf-8")

        s = UTXOMetadataStore(path=path)
        s.load()
        assert s.foreign_addr_lines == [foreign]
        assert s.get_used_addresses() == set()

        # Adding our own record must not drop the foreign one.
        s.mark_address_used("bcrt1qours", "deposit")
        s2 = UTXOMetadataStore(path=path)
        s2.load()
        assert s2.foreign_addr_lines == [foreign]


class TestReservedAddresses:
    """Tests for reserved (set-aside) deposit addresses."""

    def test_reserve_and_persist(self, tmp_path):
        path = tmp_path / "m.jsonl"
        s = UTXOMetadataStore(path=path)
        s.load()
        assert s.reserve_address("bcrt1qa", "Alice") is True
        # Idempotent: same label -> no disk write.
        assert s.reserve_address("bcrt1qa", "Alice") is False
        # Changing the label updates the record.
        assert s.reserve_address("bcrt1qa", "Bob") is True

        s2 = UTXOMetadataStore(path=path)
        s2.load()
        assert s2.get_reserved_addresses() == {"bcrt1qa"}
        assert s2.get_reserved_labels() == {"bcrt1qa": "Bob"}
        assert s2.is_address_reserved("bcrt1qa")
        assert not s2.is_address_reserved("bcrt1qother")

    def test_reserve_without_label(self, tmp_path):
        path = tmp_path / "m.jsonl"
        s = UTXOMetadataStore(path=path)
        s.load()
        s.reserve_address("bcrt1qa")
        s2 = UTXOMetadataStore(path=path)
        s2.load()
        assert s2.get_reserved_labels() == {"bcrt1qa": ""}
        # The on-disk label is the bare prefix (no trailing colon).
        assert s2.reserved_records["bcrt1qa"].label == "jm:reserved"

    def test_unreserve(self, tmp_path):
        path = tmp_path / "m.jsonl"
        s = UTXOMetadataStore(path=path)
        s.load()
        s.reserve_address("bcrt1qa", "Alice")
        assert s.unreserve_address("bcrt1qa") is True
        assert s.unreserve_address("bcrt1qa") is False
        s2 = UTXOMetadataStore(path=path)
        s2.load()
        assert s2.get_reserved_addresses() == set()

    def test_label_with_special_characters_round_trip(self, tmp_path):
        path = tmp_path / "m.jsonl"
        s = UTXOMetadataStore(path=path)
        s.load()
        s.reserve_address("bcrt1qa", "Alice: rent, March")
        s2 = UTXOMetadataStore(path=path)
        s2.load()
        assert s2.get_reserved_labels() == {"bcrt1qa": "Alice: rent, March"}

    def test_reserved_and_used_coexist(self, tmp_path):
        """An address may be both reserved and (later) on-chain used."""
        path = tmp_path / "m.jsonl"
        s = UTXOMetadataStore(path=path)
        s.load()
        s.reserve_address("bcrt1qa", "Alice")
        s.mark_address_used("bcrt1qa", "deposit")
        s2 = UTXOMetadataStore(path=path)
        s2.load()
        assert s2.get_reserved_labels() == {"bcrt1qa": "Alice"}
        assert s2.get_used_addresses() == {"bcrt1qa"}

    def test_reserved_not_treated_as_used_or_foreign(self, tmp_path):
        path = tmp_path / "m.jsonl"
        s = UTXOMetadataStore(path=path)
        s.load()
        s.reserve_address("bcrt1qa", "Alice")
        s2 = UTXOMetadataStore(path=path)
        s2.load()
        assert s2.get_used_addresses() == set()
        assert s2.foreign_addr_lines == []


class TestCoinjoinAddressTypes:
    """Tests for the import-label readers used by the wallet display."""

    def test_get_address_origins(self, tmp_path):
        s = UTXOMetadataStore(path=tmp_path / "m.jsonl")
        s.load()
        s.mark_address_used("bcrt1qa", "cj_out")
        assert s.get_address_origins("bcrt1qa") == {"cj_out"}
        # Unknown address has no origins.
        assert s.get_address_origins("bcrt1qmissing") == set()

    def test_coinjoin_types_map_origins_to_display_vocabulary(self, tmp_path):
        s = UTXOMetadataStore(path=tmp_path / "m.jsonl")
        s.load()
        s.mark_address_used("bcrt1qcjout", "cj_out")
        s.mark_address_used("bcrt1qcjchange", "cj_change")
        s.mark_address_used("bcrt1qdeposit", "deposit")
        s.mark_address_used("bcrt1qplain")  # no origin

        types = s.get_coinjoin_address_types()
        # cj_change maps to the "change" vocabulary used by get_address_history_types.
        assert types == {"bcrt1qcjout": "cj_out", "bcrt1qcjchange": "change"}

    def test_coinjoin_types_prefers_cj_out_when_both_present(self, tmp_path):
        s = UTXOMetadataStore(path=tmp_path / "m.jsonl")
        s.load()
        s.mark_address_used("bcrt1qreused", "cj_change")
        s.mark_address_used("bcrt1qreused", "cj_out")
        assert s.get_coinjoin_address_types() == {"bcrt1qreused": "cj_out"}


# ---------------------------------------------------------------------------
# Per-wallet partitioning + legacy shared-file migration
# ---------------------------------------------------------------------------


class TestPerWalletPartitioning:
    """``load_metadata_store`` partitions the BIP-329 file per wallet.

    Pre-0.30.0 builds wrote a single ``wallet_metadata.jsonl`` per data
    directory, leaking one wallet's used-address set and frozen-UTXO state
    into any other wallet opened in the same directory. The new path
    ``wallet_metadata_<fp>.jsonl`` guarantees wallet isolation, and a
    one-shot migration extracts each wallet's records from the legacy
    shared file the first time that wallet opens.
    """

    FP_A = "aabbccdd"
    FP_B = "11223344"

    def _seed_shared(self, data_dir, addr_records, outputs=None, foreign=None):
        shared = data_dir / "wallet_metadata.jsonl"
        lines = []
        for addr, origin in addr_records:
            lines.append(
                json.dumps(
                    {
                        "type": "addr",
                        "ref": addr,
                        "label": f"{USED_LABEL_PREFIX}:{origin}",
                    }
                )
            )
        for out_ref, spendable in outputs or []:
            lines.append(json.dumps({"type": "output", "ref": out_ref, "spendable": spendable}))
        for raw in foreign or []:
            lines.append(json.dumps(raw))
        shared.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return shared

    def test_uses_per_wallet_path_when_fingerprint_given(self, tmp_path):
        store = load_metadata_store(tmp_path, fingerprint=self.FP_A)
        assert store.path == tmp_path / f"wallet_metadata_{self.FP_A}.jsonl"

    def test_falls_back_to_shared_path_without_fingerprint(self, tmp_path):
        store = load_metadata_store(tmp_path)
        assert store.path == tmp_path / "wallet_metadata.jsonl"

    def test_invalid_fingerprint_falls_back_to_shared(self, tmp_path):
        store = load_metadata_store(tmp_path, fingerprint="not-hex!")
        assert store.path == tmp_path / "wallet_metadata.jsonl"

    def test_migration_filters_addr_records_to_owned_only(self, tmp_path):
        """Only ``addr`` records whose ref is in ``owned_addresses`` migrate.

        A wallet opening a data_dir that previously held another wallet's
        addr records must not inherit them.
        """
        self._seed_shared(
            tmp_path,
            addr_records=[("bcrt1qA", "deposit"), ("bcrt1qOTHER", "cj_out")],
        )

        store = load_metadata_store(
            tmp_path,
            fingerprint=self.FP_A,
            owned_addresses={"bcrt1qA"},
        )

        used = store.get_used_addresses()
        assert "bcrt1qA" in used
        assert "bcrt1qOTHER" not in used, "Migration must not import another wallet's addr records"

    def test_migration_copies_output_records_unfiltered(self, tmp_path):
        """``output`` records carry no wallet linkage in BIP-329 so we copy all.

        They are inert in wallets that do not own the underlying UTXO,
        and dropping them would lose the current wallet's frozen-state.
        """
        self._seed_shared(
            tmp_path,
            addr_records=[],
            outputs=[("aa:0", False), ("bb:1", False)],
        )

        store = load_metadata_store(
            tmp_path,
            fingerprint=self.FP_A,
            owned_addresses=set(),
        )

        assert store.is_frozen("aa:0")
        assert store.is_frozen("bb:1")

    def test_two_wallets_in_same_dir_stay_isolated(self, tmp_path):
        """Two wallets sharing a data dir each see only their own addr records.

        This is the privacy regression introduced by the shared file: it
        must be impossible after migration for wallet B to load wallet
        A's used-address set, even if A's per-wallet file was already
        created.
        """
        self._seed_shared(
            tmp_path,
            addr_records=[("bcrt1qA", "deposit"), ("bcrt1qB", "deposit")],
        )

        # Wallet A opens first.
        store_a = load_metadata_store(
            tmp_path,
            fingerprint=self.FP_A,
            owned_addresses={"bcrt1qA"},
        )
        # Wallet B opens later; the shared file is preserved so its
        # migration still finds its own records.
        store_b = load_metadata_store(
            tmp_path,
            fingerprint=self.FP_B,
            owned_addresses={"bcrt1qB"},
        )

        assert store_a.get_used_addresses() == {"bcrt1qA"}
        assert store_b.get_used_addresses() == {"bcrt1qB"}
        assert store_a.path != store_b.path

    def test_migration_skipped_when_per_wallet_file_already_exists(self, tmp_path):
        """The migration is one-shot. Second open does not re-import.

        If a user manually edits their per-wallet file (e.g. clears a
        stale entry) we must not silently re-add records from the
        shared file on the next open.
        """
        self._seed_shared(
            tmp_path,
            addr_records=[("bcrt1qA", "deposit")],
        )

        # First open: migration runs.
        load_metadata_store(tmp_path, fingerprint=self.FP_A, owned_addresses={"bcrt1qA"})
        per_wallet = tmp_path / f"wallet_metadata_{self.FP_A}.jsonl"
        # Simulate the user clearing the entry.
        per_wallet.write_text("", encoding="utf-8")

        # Second open: must NOT re-import from shared file.
        store = load_metadata_store(tmp_path, fingerprint=self.FP_A, owned_addresses={"bcrt1qA"})
        assert store.get_used_addresses() == set()

    def test_migration_preserves_shared_file(self, tmp_path):
        """The legacy shared file must survive so other wallets can migrate."""
        shared = self._seed_shared(
            tmp_path,
            addr_records=[("bcrt1qA", "deposit"), ("bcrt1qB", "deposit")],
        )
        original = shared.read_text(encoding="utf-8")

        load_metadata_store(tmp_path, fingerprint=self.FP_A, owned_addresses={"bcrt1qA"})

        assert shared.exists()
        assert shared.read_text(encoding="utf-8") == original

    def test_migration_without_owned_addresses_skips_all_addr(self, tmp_path):
        """When the caller cannot supply ownership info, no addr records leak.

        The safer default is to drop all addr records and let the
        wallet's own sync re-populate from on-chain data, rather than
        risk inheriting another wallet's used set.
        """
        self._seed_shared(
            tmp_path,
            addr_records=[("bcrt1qA", "deposit"), ("bcrt1qB", "deposit")],
            outputs=[("aa:0", False)],
        )

        store = load_metadata_store(tmp_path, fingerprint=self.FP_A, owned_addresses=None)

        assert store.get_used_addresses() == set()
        # output records are still copied (frozen-state preservation).
        assert store.is_frozen("aa:0")

    def test_no_shared_file_no_migration(self, tmp_path):
        """Brand new data_dir: no legacy file, no migration, empty store."""
        store = load_metadata_store(tmp_path, fingerprint=self.FP_A, owned_addresses={"bcrt1qA"})
        assert store.get_used_addresses() == set()
        # Per-wallet file is not created until a save happens.
        assert not (tmp_path / f"wallet_metadata_{self.FP_A}.jsonl").exists()


# ---------------------------------------------------------------------------
# Temporary CoinJoin lock tests
# ---------------------------------------------------------------------------


class TestCoinJoinLocks:
    """Tests for the persisted, self-expiring CoinJoin UTXO locks.

    These prevent two concurrent rounds (maker or taker, same or different
    process) from committing the same input and producing conflicting,
    mutually double-spending transactions.
    """

    @pytest.fixture
    def store_path(self, tmp_path):
        return tmp_path / "wallet_metadata.jsonl"

    @pytest.fixture
    def a(self):
        return "aa" * 32 + ":0"

    @pytest.fixture
    def b(self):
        return "bb" * 32 + ":1"

    @pytest.fixture
    def c(self):
        return "cc" * 32 + ":2"

    def test_lock_and_query(self, store_path, a, b):
        store = UTXOMetadataStore(path=store_path)
        store.load()
        assert store.try_lock_outpoints([a, b], ttl=100) is True
        assert store.get_locked_outpoints() == {a, b}

    def test_lock_is_cross_process_visible(self, store_path, a):
        s1 = UTXOMetadataStore(path=store_path)
        s1.load()
        assert s1.try_lock_outpoints([a], ttl=100) is True
        # A separate store instance (simulating another process) sees the lock.
        s2 = UTXOMetadataStore(path=store_path)
        s2.load()
        assert s2.get_locked_outpoints() == {a}

    def test_lock_conflict_on_already_locked(self, store_path, a):
        s1 = UTXOMetadataStore(path=store_path)
        s1.load()
        assert s1.try_lock_outpoints([a], ttl=100) is True
        s2 = UTXOMetadataStore(path=store_path)
        s2.load()
        assert s2.try_lock_outpoints([a]) is False

    def test_lock_conflict_on_frozen(self, store_path, c):
        store = UTXOMetadataStore(path=store_path)
        store.load()
        store.freeze(c)
        assert store.try_lock_outpoints([c]) is False

    def test_lock_is_all_or_nothing(self, store_path, a, b):
        s1 = UTXOMetadataStore(path=store_path)
        s1.load()
        assert s1.try_lock_outpoints([a], ttl=100) is True
        s2 = UTXOMetadataStore(path=store_path)
        s2.load()
        # b is free but a is locked -> the whole request fails, b stays free.
        assert s2.try_lock_outpoints([a, b]) is False
        s3 = UTXOMetadataStore(path=store_path)
        s3.load()
        assert b not in s3.get_locked_outpoints()

    def test_release(self, store_path, a, b):
        store = UTXOMetadataStore(path=store_path)
        store.load()
        store.try_lock_outpoints([a, b], ttl=100)
        store.release_outpoints([a])
        reloaded = UTXOMetadataStore(path=store_path)
        reloaded.load()
        assert reloaded.get_locked_outpoints() == {b}

    def test_lock_auto_expires(self, store_path, a):
        store = UTXOMetadataStore(path=store_path)
        store.load()
        # Expire in the past.
        store.try_lock_outpoints([a], ttl=-1)
        assert store.get_locked_outpoints() == set()
        # Expired lock can be re-acquired.
        assert store.try_lock_outpoints([a], ttl=100) is True

    def test_freeze_survives_lock_expiry(self, store_path, c):
        store = UTXOMetadataStore(path=store_path)
        store.load()
        store.freeze(c)
        store.try_lock_outpoints([c], ttl=100)  # no-op: frozen -> conflict
        # Even an expired lock elsewhere must never clear a user freeze.
        store.records[c].lock_until = 1.0  # already expired
        store._prune_expired_locks(now=2.0)
        assert store.is_frozen(c)

    def test_lock_only_record_persisted_and_reloaded(self, store_path, a):
        store = UTXOMetadataStore(path=store_path)
        store.load()
        store.try_lock_outpoints([a], ttl=100)
        # A record carrying only a lock (no freeze/label) must round-trip.
        reloaded = UTXOMetadataStore(path=store_path)
        reloaded.load()
        assert a in reloaded.get_locked_outpoints()
        assert reloaded.records[a].spendable is None
        assert reloaded.records[a].label is None

    def test_lock_field_is_bip329_extension(self, store_path, a):
        import json as _json

        store = UTXOMetadataStore(path=store_path)
        store.load()
        store.try_lock_outpoints([a], ttl=100)
        lines = [_json.loads(line) for line in store_path.read_text().splitlines() if line.strip()]
        rec = next(r for r in lines if r.get("ref") == a)
        assert rec["type"] == "output"
        assert "jm_lock_until" in rec

"""Pinned model/metadata patch and biography selection behavior."""

from __future__ import annotations

import ast
import sys
from collections.abc import Sequence
from dataclasses import dataclass, fields
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "music_assistant_lm" / "patches"))
import artist_bio_provenance as bio  # noqa: E402

MODEL = ROOT / "tests" / "fixtures" / "music_assistant_models_metadata_1_1_205.py"
ENRICHMENT = ROOT / "tests" / "fixtures" / "metadata_enrichment_2_10_4.py"
ARTISTS = ROOT / "tests" / "fixtures" / "artists_2_10_4.py"


def _selector(source: str):
    """Execute only the patched pure selection method from the pinned module."""
    tree = ast.parse(source)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "MetadataEnrichmentMixin")
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "_select_description")
    namespace: dict = {"Sequence": Sequence}
    selector_class = ast.ClassDef(name="Selector", bases=[], keywords=[], body=[method], decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[selector_class], type_ignores=[]))
    exec(compile(module, "selector", "exec"), namespace)  # noqa: S102 - isolated pure method from pinned source
    selector = namespace["Selector"]()
    selector.preferred_language = "fr"
    return selector._select_description


def _metadata_class(source: str):
    """Run the pinned model's real merge method on a minimal dataclass."""
    tree = ast.parse(source)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "MediaItemMetadata")
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "update")
    method.returns = None
    for argument in (*method.args.posonlyargs, *method.args.args, *method.args.kwonlyargs):
        argument.annotation = None
    namespace = {"fields": fields, "UniqueList": list, "merge_lists": lambda old, new: old + new}
    module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
    exec(compile(module, "metadata_merge", "exec"), namespace)  # noqa: S102 - pinned pure method

    @dataclass
    class Metadata:
        description: str | None = None
        description_language: str | None = None
        description_source: str | None = None
        description_observed_at: int | None = None
        last_refresh: int | None = None

    Metadata.update = namespace["update"]
    return Metadata


def test_pinned_artist_bio_patch_is_idempotent_and_preserves_model_fields() -> None:
    model = bio.apply(MODEL.read_text(encoding="utf-8"), bio.MODEL)
    enrichment = bio.apply(ENRICHMENT.read_text(encoding="utf-8"), bio.ENRICHMENT)
    artists = bio.apply(ARTISTS.read_text(encoding="utf-8"), bio.ARTISTS)
    assert bio.apply(model, bio.MODEL) == model
    assert bio.apply(enrichment, bio.ENRICHMENT) == enrichment
    assert bio.apply(artists, bio.ARTISTS) == artists
    assert "description_source: str | None = None" in model
    assert "description_observed_at: int | None = None" in model
    assert '"description_source", "description_observed_at"):' in model
    assert "artist.metadata.description_observed_at" in enrichment
    assert 'metadata.description_source = ("manual" if metadata.description else None)' in artists
    assert "patches/artist_bio_provenance.py" in (ROOT / "music_assistant_lm" / "Dockerfile").read_text()


def test_bio_source_follows_language_selection_and_preserved_copy() -> None:
    patched = bio.apply(ENRICHMENT.read_text(encoding="utf-8"), bio.ENRICHMENT)
    select = _selector(patched)
    candidates = [("en", "English", "spotify--one"), ("fr", "French", "wikipedia--two")]
    assert select(candidates, None, None, None) == ("French", "fr", "wikipedia--two", True)
    assert select([candidates[0]], "Saved French", "fr", "qobuz--three") == (
        "Saved French", "fr", "qobuz--three", False
    )
    assert select([candidates[0]], None, None, None) == ("English", "en", "spotify--one", True)
    assert select([], "Saved", "es", "local--four") == ("Saved", "es", "local--four", False)
    assert select(candidates, "My biography", "es", "manual") == (
        "My biography", "es", "manual", False
    )


def test_biography_provenance_persists_and_manual_override_wins() -> None:
    model = bio.apply(MODEL.read_text(encoding="utf-8"), bio.MODEL)
    Metadata = _metadata_class(model)
    stored = Metadata("Old", "en", "provider-a", 100, 100)
    stored.update(Metadata("New", "en", "provider-b", 200, 200))
    assert (stored.description, stored.description_source, stored.description_observed_at) == (
        "New", "provider-b", 200
    )
    manual = Metadata("Mine", "en", "manual", 300, 300)
    manual.update(Metadata("Provider text", "fr", "provider-c", 400, 400))
    assert (manual.description, manual.description_source, manual.description_observed_at) == (
        "Mine", "manual", 300
    )
    assert manual.last_refresh == 400


def test_pinned_patch_rejects_unknown_model_anchor() -> None:
    with pytest.raises(SystemExit, match="anchor"):
        bio.apply("class MediaItemMetadata: pass", bio.MODEL)

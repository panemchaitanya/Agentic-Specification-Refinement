"""Phase 4 reporting, LaTeX table generation, and reproducible release packaging."""

from __future__ import annotations

from reqhq.reporting.generator import (
    DRY_RUN_BANNER_MD,
    DRY_RUN_BANNER_TEX,
    EXPECTED_SVGS,
    QUARANTINED_STATUS,
    TAXONOMY_CATEGORIES,
    TEAM_AUTHORS,
    TEAM_INSTITUTION,
    TEAM_NAME,
    escape_latex,
    generate_final_report,
)
from reqhq.reporting.models import (
    ArtifactsManifest,
    BundleVerificationResult,
    ReleaseArtifactEntry,
    ReportGenerationResult,
    VerdictStatus,
)
from reqhq.reporting.packager import (
    MANIFEST_FILENAME,
    BundleTamperError,
    package_release_bundle,
    verify_bundle,
)

__all__ = [
    "DRY_RUN_BANNER_MD",
    "DRY_RUN_BANNER_TEX",
    "EXPECTED_SVGS",
    "MANIFEST_FILENAME",
    "QUARANTINED_STATUS",
    "TAXONOMY_CATEGORIES",
    "TEAM_AUTHORS",
    "TEAM_INSTITUTION",
    "TEAM_NAME",
    "ArtifactsManifest",
    "BundleTamperError",
    "BundleVerificationResult",
    "ReleaseArtifactEntry",
    "ReportGenerationResult",
    "VerdictStatus",
    "escape_latex",
    "generate_final_report",
    "package_release_bundle",
    "verify_bundle",
]

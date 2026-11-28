"""Unit & deterministic Hypothesis PBT tests for standalone PDF/SRS text cleaner & review UI."""

from __future__ import annotations

import json
from pathlib import Path

from hypothesis import given, settings, strategies as st
import pytest
from typer.testing import CliRunner

from reqhq.cli import app
from reqhq.ingestion import (
    CleanerConfig,
    NoiseCategory,
    PDFTextCleaner,
    render_cleaning_review_ui,
    select_stratified_files,
)
from reqhq.ingestion.cleaner import (
    _atomic_write_lf,
    _hoist_mid_block_row_numbers_in_block,
    _is_heading_line,
    _try_reconstruct_pipe_table,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PURE_DEV_DIR = REPO_ROOT / "data" / "frozen" / "pure" / "text" / "dev"


class TestPDFTextCleanerRules:
    """Targeted unit tests covering every deterministic rule, edge case, and safety shield."""

    def test_cleans_all_noise_categories_while_protecting_normative_requirements(self) -> None:
        raw = (
            "MY PROJECT TITLE\n"
            "Version: 1.0\n"
            "December 15, 2005\n"
            "Brooksville, FL 34604-6899\n"
            "Confidential \uf0d3 Acme Corp, 2005\n"
            "________________________________________\n"
            "\n\n\n"
            "Table of Contents\n"
            "1. Introduction ..................... 1\n"
            "2. Requirements     3\n"
            "3.1.1 The system must enforce strict quotas.    4\n"
            "3.1.2 The system must provide wrapped TOC\n"
            "      entries across lines.    5\n"
            "A1 Registration\n"
            "Revision History\n"
            "v1.0  2005-12-01  Initial draft\n"
            "1. Introduction\n"
            "This specification defines the complete operational behavior of the telemetry platform.\n"
            "The system shall authenticate operators using multi-factor credentials on every session.\n"
            "   z Reference URL preserved:\n"
            "     http://www.cs.utexas.edu/users/s2s/admin/s2s-by-semester.html\n"
            "Back to Contents\n"
            "Acme Corp Running Header\n"
            "http://example.edu/srs.html    1/22/2003\n"
            "Page 1 of 4\n"
            "\x0cAcme Corp Running Header\n"
            "1 - 2\n"
            "The system shall support auto-\n"
            "mated failover across regional zones.\n"
            "This is a deliberately long wrapped specification sentence that continues onto the\n"
            "District level with capitalized proper nouns too.\n"
            "The system should bridge sentences split across\n"
            "2\n"
            "\x0cAcme Corp Running Header\n"
            "the page boundary cleanly.\n\n"
            "Name            Role                              Title\n"
            "Bruce Wirth     Executive level view of           Deputy Director\n"
            "                requirements.\n\n"
            " Access Module\n"
            "               The System must allow users to limit access to\n"
            "     1.\n"
            "               specified user groups.\n\n"
            "               The System must export audit trails for cases (without\n"
            "     2.        affecting stored trails) for external auditors.\n"
            "| Col A | Col B |\n"
            "3\n"
            "\x0cAcme Corp Running Header\n"
            "The system must enforce role-based access control on all endpoints.\n"
            "4\n"
        )
        cleaner = PDFTextCleaner()
        res = cleaner.clean_text(raw, doc_id="synthetic-srs", source_path="synth.txt")

        assert res.page_count == 4
        assert res.normative_removed_count == 0
        assert res.reduction_pct > 20.0
        assert "The system shall authenticate operators using multi-factor credentials" in res.cleaned_text
        assert "automated failover across regional zones." in res.cleaned_text
        assert "onto the District level with capitalized proper nouns too." in res.cleaned_text
        assert "split across the page boundary cleanly." in res.cleaned_text
        assert "http://www.cs.utexas.edu/users/s2s/admin/s2s-by-semester.html" in res.cleaned_text
        assert "http://example.edu/srs.html" not in res.cleaned_text
        assert "| Bruce Wirth | Executive level view of requirements. | Deputy Director |" in res.cleaned_text
        assert "1. The System must allow users to limit access to specified user groups." in res.cleaned_text
        assert "2. The System must export audit trails for cases (without affecting stored trails) for external auditors." in res.cleaned_text
        assert "| Col A | Col B |" in res.cleaned_text
        assert "Acme Corp Running Header" not in res.cleaned_text
        assert "Back to Contents" not in res.cleaned_text
        assert "Table of Contents" not in res.cleaned_text
        assert "Revision History" not in res.cleaned_text
        assert "1 - 2" not in res.cleaned_text

        cats = set(res.category_counts.keys())
        for expected_cat in (
            NoiseCategory.REPEATED_HEADER_FOOTER,
            NoiseCategory.PAGE_NUMBER_STAMP,
            NoiseCategory.WEB_PRINT_NAVIGATION,
            NoiseCategory.TOC_AND_LISTS,
            NoiseCategory.REVISION_AND_DOC_HISTORY,
            NoiseCategory.COPYRIGHT_AND_LEGAL,
            NoiseCategory.COVER_FRONTMATTER,
            NoiseCategory.DECORATIVE_RULE,
            NoiseCategory.EXCESS_WHITESPACE,
            NoiseCategory.PDF_REFLOW_NORMALIZATION,
            NoiseCategory.TABLE_RECONSTRUCTION,
        ):
            assert expected_cat.value in cats

    def test_empty_input_and_toggles_off(self) -> None:
        cleaner_default = PDFTextCleaner()
        empty = cleaner_default.clean_text("")
        assert empty.raw_chars == 0
        assert empty.cleaned_chars == 0
        assert empty.reduction_pct == 0.0
        assert empty.est_chunks_before == 0

        cfg_off = CleanerConfig(
            strip_repeated_edges=False,
            strip_page_numbers=False,
            strip_web_nav=False,
            strip_toc_blocks=False,
            strip_revision_history=False,
            strip_copyright_legal=False,
            strip_cover_frontmatter=False,
            strip_decorative_rules=False,
            collapse_whitespace=False,
            reconstruct_tables=False,
            hoist_mid_block_row_numbers=False,
            reflow_pdf_wraps=False,
        )
        res = PDFTextCleaner(cfg_off).clean_text("\n\x0cLine A\n\n\nLine B\n")
        assert res.page_count == 2
        assert "Line A" in res.cleaned_text and "Line B" in res.cleaned_text

        # Also exercise reflow_pdf_wraps=False with hoisted row numbers
        cfg_hoist_only = CleanerConfig(reflow_pdf_wraps=False, reconstruct_tables=False)
        res_h = PDFTextCleaner(cfg_hoist_only).clean_text(
            "              The System must preserve audit logs across\n"
            "     4.\n"
            "              all restarts.\n"
        )
        assert "4. The System must preserve audit logs across" in res_h.cleaned_text
        assert NoiseCategory.PDF_REFLOW_NORMALIZATION.value in res_h.category_counts

    def test_heading_table_and_row_hoisting_helper_edge_cases(self) -> None:
        assert _is_heading_line("", prev_blank=True, next_blank=True) is False
        assert _is_heading_line("# Markdown Heading", prev_blank=False, next_blank=False) is True
        assert _is_heading_line("ALL CAPS BANNER", prev_blank=False, next_blank=False) is True
        assert _is_heading_line("Access Module", prev_blank=True, next_blank=False) is True
        assert _is_heading_line("4. The System must allow login.", prev_blank=True, next_blank=False) is False
        assert _is_heading_line("This system will allow access", prev_blank=True, next_blank=False) is False

        # Row hoisting edge cases
        assert _hoist_mid_block_row_numbers_in_block(["single"]) == (["single"], 0)
        assert _hoist_mid_block_row_numbers_in_block(["a", "b"]) == (["a", "b"], 0)
        already = ["       1. Already numbered", "       continuation"]
        assert _hoist_mid_block_row_numbers_in_block(already) == (already, 0)

        # Pipe table reconstruction edge cases
        assert _try_reconstruct_pipe_table(["only one"]) == (None, 0)
        # Full-width prose rejected
        assert _try_reconstruct_pipe_table(["X" * 70, "Col1    Col2"]) == (None, 0)
        # Bullet list rejected
        assert _try_reconstruct_pipe_table(["- item1    val1", "- item2    val2"]) == (None, 0)
        # Misaligned columns (justified prose) rejected
        assert _try_reconstruct_pipe_table(["A   B", "LongerWordHere             C", "Short    D"]) == (None, 0)
        # Vertically-centered glossary table with wrapped col-0 ("Macromedia" + "Flash")
        glossary_block = [
            "      Term            Definition",
            "                      Hypertext Markup Language: used",
            "      HTML",
            "                      to describe web pages.",
            "      iMac            A type of computer.",
            "                      An application for creating dynamic",
            "      Macromedia",
            "      Flash",
            "                      graphics over the web.",
            "      ",
        ]
        md_tbl, n_rows = _try_reconstruct_pipe_table(glossary_block)
        assert md_tbl is not None and n_rows == 4
        assert "| HTML | Hypertext Markup Language: used to describe web pages. |" in md_tbl
        assert "| Macromedia Flash | An application for creating dynamic graphics over the web. |" in md_tbl

        # Degenerate single collapsed row after merges returns (None, 0)
        single_merged = ["   A    B", "        C", "   X    Y"]
        assert _try_reconstruct_pipe_table(single_merged)[0] is not None
        only_header_plus_cont = ["   ColA      ColB", "   ColA2     ColB2"]
        assert _try_reconstruct_pipe_table(only_header_plus_cont)[0] is not None

    def test_edge_signature_filters_and_toc_exit_branches(self) -> None:
        long_edge = "X" * 170
        pages = [
            (
                f"{long_edge}\n"
                "A\n"
                "1.\n"
                "Req ID   Requirement Statement   Use Case\n"
                "The system shall remain online.\n"
                "Common Header\n"
                "Table of Contents\n"
                "1.0 Overview\n"
                "1.1 Scope\n"
                "1.0 Overview\n"
                "Table of Contents\n"
                "The system shall immediately exit TOC on normative line.\n"
                "Table of Contents\n"
                "1. Introduction\n"
                "This paragraph has more than eight words and clearly starts the body.\n"
                "Table of Contents\n"
                "This body sentence has enough words to exit a table of contents immediately.\n"
                "Table of Contents\n"
                "UnstructuredNonTocToken\n"
                "Table of Contents\n"
                "2.0 Wrapped Check\n\n"
                "2.1 Other Check\n"
                "Not a page number line\n"
                "Table of Contents\n"
                "1. Overview\n"
                "Common Header\n"
                "alpha\n"
                "beta\n"
                "gamma\n"
                "delta\n"
                "epsilon\n"
                "zeta\n"
                "Revision History\n"
                + "".join(f"rev entry {i}\n" for i in range(85))
                + "Regular line after cap\n"
            ),
            "\n\n",
            "Common Header\nBody page 3\n",
            "Common Header\nBody page 4\n",
        ]
        raw = "\x0c".join(pages)
        res = PDFTextCleaner().clean_text(raw, doc_id="edge-test")
        assert res.page_count == 4
        assert res.normative_removed_count == 0
        assert "The system shall remain online." in res.cleaned_text
        assert "The system shall immediately exit TOC on normative line." in res.cleaned_text
        assert "1.0 Overview" in res.cleaned_text
        assert "Regular line after cap" in res.cleaned_text

    def test_toc_exits_on_revision_heading_and_head_lookahead_eof(self) -> None:
        raw = (
            "Table of Contents\n"
            "1. Introduction\n"
            "2. Overview\n"
            "3. Details ..... 5\n"
            "Table of Contents\n"
            "Revision History\n"
            "v1.0 initial\n"
            "2. Architecture\n"
            "Table of Contents\n"
            "9. Appendix"
        )
        res = PDFTextCleaner().clean_text(raw, doc_id="toc-rev-eof")
        assert "2. Architecture" in res.cleaned_text

    def test_clean_file_batch_ui_and_stratified_selector(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="Input file not found"):
            PDFTextCleaner().clean_file(tmp_path / "missing.txt")

        with pytest.raises(ValueError, match="at least one input file"):
            PDFTextCleaner().clean_batch([], tmp_path / "empty_out")

        with pytest.raises(ValueError, match="limit must be >= 1"):
            select_stratified_files(tmp_path, limit=0)

        with pytest.raises(FileNotFoundError, match="Input path not found"):
            select_stratified_files(tmp_path / "nope")

        empty_dir = tmp_path / "empty_dir"
        empty_dir.mkdir()
        with pytest.raises(ValueError, match="No .txt or .md files found"):
            select_stratified_files(empty_dir)

        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        for i in range(4):
            (docs_dir / f"doc_{i}.txt").write_text(
                f"Header {i}\n" + ("The system shall log every event.\n" * (i + 1)),
                encoding="utf-8",
                newline="\n",
            )

        assert len(select_stratified_files(docs_dir / "doc_0.txt")) == 1
        assert len(select_stratified_files(docs_dir, limit=10)) == 4
        assert len(select_stratified_files(docs_dir, limit=2, stratified=False)) == 2
        assert len(select_stratified_files(docs_dir, limit=1, stratified=True)) == 1
        assert len(select_stratified_files(docs_dir, limit=3, stratified=True)) == 3

        out_dir = tmp_path / "batch_out"
        summary = PDFTextCleaner().clean_batch(
            select_stratified_files(docs_dir, limit=2),
            out_dir,
            generate_ui=True,
        )
        assert summary.total_docs == 2
        assert summary.review_ui_path is not None
        ui_file = Path(summary.review_ui_path)
        assert ui_file.is_file()
        assert b"\r\n" not in ui_file.read_bytes()
        assert "ReqHQ PDF Cleaner Verification Studio v2" in ui_file.read_text(encoding="utf-8")

        summary_no_ui = PDFTextCleaner().clean_batch(
            [docs_dir / "doc_0.txt"], tmp_path / "batch_no_ui", generate_ui=False
        )
        assert summary_no_ui.review_ui_path is None
        lf_file = tmp_path / "lf_test.txt"
        _atomic_write_lf(lf_file, "already ends with newline\n")
        assert lf_file.read_text(encoding="utf-8") == "already ends with newline\n"

    def test_real_pure_dev_regression_on_all_5_user_findings(self) -> None:
        cleaner = PDFTextCleaner()

        # Finding #3 & #4: space fractions preserves team URL, glossary table, and consecutive headings
        sf = cleaner.clean_file(PURE_DEV_DIR / "2001 - space fractions.pdf.txt")
        assert "http://www.cs.utexas.edu/users/s2s/admin/s2s-by-semester.html" in sf.cleaned_text
        assert "1.0 Introduction\n1.1 Purpose of this Document" in sf.cleaned_text
        assert "| HTML | Hypertext Markup Language:" in sf.cleaned_text
        assert sf.normative_removed_count == 0

        # Finding #4: puget sound deletes full TOC (including 3.1.1 .. 5.) and keeps body 1. Introduction
        ps = cleaner.clean_file(PURE_DEV_DIR / "2007 - puget sound.pdf.txt")
        assert "Table of Contents" not in ps.cleaned_text
        assert "1. Introduction\n1.1 Purpose" in ps.cleaned_text
        assert ps.normative_removed_count == 0

        # Finding #5: cctns hoists mid-sentence NFR row numbers & bridges cross-page sentence
        cc = cleaner.clean_file(PURE_DEV_DIR / "0000 - cctns.pdf.txt")
        assert "1. The System must allow the user to limit access to cases to specified users or user groups." in cc.cleaned_text
        assert "2. The system should provide for role-based control for the functionality within the system." in cc.cleaned_text
        assert cc.normative_removed_count == 0

        # Finding #1: water use reconstructs Stakeholder Summary pipe table cleanly
        wu = cleaner.clean_file(PURE_DEV_DIR / "2007 - water use.pdf.txt")
        assert "| Name | Role | Title |" in wu.cleaned_text
        assert "| Bruce Wirth | Executive level view of requirements. | Deputy Executive Director |" in wu.cleaned_text
        assert wu.normative_removed_count == 0

        ui_html = render_cleaning_review_ui([cc], raw_texts_by_id={cc.doc_id: (PURE_DEV_DIR / "0000 - cctns.pdf.txt").read_text(encoding="utf-8")})
        assert "Rendered Clean View" in ui_html

    def test_cli_clean_trial_success_and_adr_guard(self, tmp_path: Path) -> None:
        runner = CliRunner()
        out_dir = tmp_path / "cli_clean_out"
        res = runner.invoke(
            app,
            [
                "clean",
                "trial",
                "--in",
                str(PURE_DEV_DIR / "0000 - cctns.pdf.txt"),
                "--out",
                str(out_dir),
                "--limit",
                "1",
            ],
        )
        assert res.exit_code == 0, res.output
        assert "Cleaning trial complete: docs=1" in res.output
        assert (out_dir / "review_ui.html").is_file()
        assert (out_dir / "batch_summary.json").is_file()

        res_err = runner.invoke(
            app,
            [
                "clean",
                "trial",
                "--in",
                str(PURE_DEV_DIR / "0000 - cctns.pdf.txt"),
                "--out",
                str(tmp_path / "docs" / "adr"),
            ],
        )
        assert res_err.exit_code == 1


class TestPDFTextCleanerPBT:
    """Deterministic Hypothesis property-based tests for cleaner invariants."""

    @settings(derandomize=True, max_examples=50)
    @given(
        st.lists(
            st.text(
                alphabet=st.characters(blacklist_categories=("Cs",)),
                min_size=0,
                max_size=60,
            ),
            min_size=0,
            max_size=30,
        ),
        st.text(
            alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters=" -"),
            min_size=6,
            max_size=40,
        ),
    )
    def test_pbt_cleaning_preserves_normative_sentences_and_bounds_invariants(
        self,
        noise_lines: list[str],
        clause_body: str,
    ) -> None:
        normative_line = f"REQ-101: The system shall enforce {clause_body.strip() or 'audit integrity'}."
        raw = "\n".join([*noise_lines[:10], normative_line, *noise_lines[10:]])
        res = PDFTextCleaner().clean_text(raw, doc_id="pbt-doc")

        assert 0.0 <= res.reduction_pct <= 100.0
        assert res.chars_saved == max(0, res.raw_chars - res.cleaned_chars)
        assert res.normative_removed_count == 0
        assert "REQ-101:" in res.cleaned_text
        assert res.est_chunks_after <= res.est_chunks_before + 1
        assert json.loads(res.model_dump_json())["doc_id"] == "pbt-doc"

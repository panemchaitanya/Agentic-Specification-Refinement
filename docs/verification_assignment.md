# Gold verification assignment — mid-submission (6 Nov 2026)

Two annotators, all 61 dev documents. Guidelines: training/ANNOTATION_GUIDELINES.md.
Tool: `python3 training/verify_suggestions.py --annotator <name> --split dev --docs <list>`
(stdlib only, Python 3.10+, resumable — rerun the same command to continue).
Output: one verdict JSON per doc under data/frozen/gold_verdicts/dev/.

Each annotator verifies their UNIQUE list plus the SHARED list. Shared docs are
verified independently by both (no peeking); pairwise Cohen's kappa on
accept/reject is computed from them at merge time (training/merge_verdicts.py),
conflicts resolve by majority vote — tie means the item stays out
(conservative). Unsure about an item? press s (skip) — skipped items stay
unverified instead of guessed.

Estimated pace: ~12 min/doc. A: 27+8=35 docs ≈ 7h. B: 26+8=34 docs ≈ 6.8h.

## Annotator A — 27 unique + 8 shared
pure-2005_-_grid_3D,pure-2007_-_get_real_0.2,pure-2009_-_video_search,pure-2003_-_qheadache,pure-2010_-_mashboot,pure-2009_-_warc_III,pure-0000_-_inventory,pure-2009_-_library,pure-0000_-_cctns,pure-2005_-_triangle,pure-2009_-_gaia,pure-2010_-_split_merge,pure-2005_-_microcare,pure-0000_-_gamma_j,pure-2004_-_sprat,pure-2004_-_e-procurement,pure-2008_-_vub,pure-2006_-_stewards,pure-2007_-_ertms,pure-2001_-_telescope,pure-2005_-_pontis,pure-2004_-_rlcs,pure-2002_-_sce_api,pure-2007_-_nde,pure-2001_-_hats,pure-2007_-_nlm,pure-2000_-_nasa_x38

## Annotator B — 26 unique + 8 shared
pure-2007_-_central_trading_system,pure-2008_-_caiso,pure-2001_-_space_fractions,pure-2005_-_nenios,pure-2004_-_colorcast,pure-2005_-_znix,pure-1999_-_dii,pure-2002_-_evla_corr,pure-2001_-_libra,pure-2010_-_gparted,pure-2008_-_peering,pure-2002_-_evla_back,pure-2004_-_ijis,pure-1999_-_multi-mahjong,pure-1998_-_themas,pure-2004_-_grid_bgc,pure-2008_-_virtual_ed,pure-2003_-_tachonet,pure-2010_-_fishing,pure-2005_-_clarus_low,pure-2007_-_mdot,pure-2004_-_jse,pure-2009_-_peppol,pure-2008_-_viper,pure-1999_-_tcs,pure-2009_-_inventory_2.0

## Shared (both annotators, independent) — stratified by document length,
## 2 of the 18 LLM-truncated docs included; also the model cross-check sample
pure-2001_-_ctc_network,pure-2003_-_pnnl,pure-2006_-_eirene_sys_15,pure-2007_-_puget_sound,pure-2007_-_water_use,pure-2009_-_peazip,pure-2010_-_home_1.3,pure-2011_-_opensg_0.1

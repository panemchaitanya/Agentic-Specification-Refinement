# Training feasibility: compute options (verified 6 Nov 2026)

No unlimited free route exists. The Stage 3 pilot (one small model on the
PROMISE classification subtask) decides what is actually feasible:
it measures quality, VRAM and training time for the chosen model size.

Grounded options, all usable at zero cost:

1. **Google Colab free tier.** Free-of-charge GPU/TPU access, but per the
   official FAQ "resources are not guaranteed and not unlimited, and
   usage limits sometimes fluctuate"; free runtimes can be terminated.
   Source: https://research.google.com/colaboratory/faq.html
2. **Kaggle Notebooks free GPU.** Free NVIDIA TESLA P100 (T4 options also
   exist); weekly GPU quota of about 30 hours ("30 hours or sometimes
   higher depending on demand"), resetting weekly.
   Source: https://www.kaggle.com/docs/efficient-gpu-usage
3. **Local CPU.** No per-call rate caps and no quotas, but slow; only
   viable for very small models or tiny pilot subsets.

Practical reading: a small-model pilot (e.g. a T5-small/BERT-mini-class
model on ~1k labelled sentences) is realistic on all three. Full SLM
critic training (H2) depends on the pilot's measured VRAM/time and on
the extra contextual labels from docs/datasets.md existing first.
Compute choice is the team/user's call once pilot numbers exist.

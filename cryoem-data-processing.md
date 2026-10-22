# Cryo-EM Data Processing Guide

This guide is a practical framework for approaching single-particle cryo-EM data processing for any protein or complex. It is written from the CryoSPARC tutorial and case-study corpus listed in the requested attachment, with emphasis on reusable decisions rather than reproducing one tutorial workflow.

The core rule is simple: start from the biology, then let diagnostics decide the processing branch. A rigid soluble enzyme, a small GPCR in detergent, a pseudosymmetric channel, a helical filament, and a high-symmetry shell with asymmetric cargo should not be processed as if they have the same failure modes.

## 1. Start With A Processing Hypothesis

Before making jobs, write down the target in processing terms:

- Expected molecular weight, particle diameter, longest dimension, and whether micelle, nanodisc, lipid, support film, or nucleic acid density should be visible.
- Domain layout: compact, elongated, hollow, membrane-embedded, peripheral domain, flexible arm, internal cargo, repeated local unit, or helical assembly.
- Expected symmetry: C1, true point symmetry, local symmetry, pseudosymmetry, symmetry mismatch, or non-point-group repetition.
- Expected heterogeneity: junk/incomplete particles, alternate species, apo/bound states, ligand occupancy, conformational states, continuous hinge motion, partial occupancy, damaged subunits, or preferred orientation.
- Prior information: AlphaFold or homolog model, published map/model, expected ligand chemistry, known stoichiometry, biochemical purification context, expected glycosylation/lipids/cofactors, and whether handedness is known.

Use this hypothesis to decide what you are trying to test at every stage. For example:

- "Are these picks real particles?" is tested by 2D classification and early ab-initio maps.
- "Is this a homogeneous particle stack?" is tested by per-particle scale, ab-initio/heterogeneous refinement, 3DVA, and 3D classification.
- "Is the ligand really present?" is tested by focused classification, difference maps, sharpening variants, and chemical plausibility.
- "Is the global resolution believable?" is tested by cFSC/cFAR, local resolution, masks, and visual map features.

## 2. Baseline Workflow

Use this as the default starting route for a new single-particle dataset. Branch only when diagnostics or sample biology justify it.

### 2.1 Import And Preprocess

1. Import movies or micrographs with correct pixel size, voltage, spherical aberration, total dose, gain reference, and gain orientation.
2. Split import jobs by collection subset when gain references, collection days, frame counts, exposure dose, pixel size, or beam shift strategy differ meaningfully.
3. For EPU AFIS or beam-shifted data, preserve beam-shift metadata and create exposure groups before high-order CTF refinement.
4. For EER movies, import with the correct EER settings and keep the eventual pixel size/upsampling choice consistent with the desired resolution.
5. For phase plate or negative stain data, mark the data type correctly at import or before downstream processing. Negative stain processing usually requires turning off CTF correction in jobs where it would otherwise be applied.
6. Run Patch Motion Correction for movies. Use float16 output unless there is a specific reason to keep 32-bit micrographs.
7. Run Patch CTF Estimation. Re-extract per-particle CTF values later if you recompute Patch CTF for already extracted particles.

Practical defaults:

- `Save results in 16-bit floating point`: true for motion-corrected micrographs and particle extraction.
- `Number of GPUs to parallelize`: use available GPUs for motion correction, CTF estimation, and large classification/refinement jobs.
- Keep raw metadata organized by collection subset. This matters later for exposure groups, CTF refinement, and troubleshooting.

### 2.2 Curate Exposures Before Picking

Do not extract particles from obviously bad micrographs if you can remove them early.

Inspect and filter:

- CTF fit resolution.
- Relative ice thickness.
- Total full-frame and local motion.
- Defocus and astigmatism outliers.
- Average intensity or gain/intensity outliers.
- Intrinsic and extrinsic ice defects, crystalline ice, ethane contamination, carbon/gold/support edges, and hole edges.
- Particle count outliers after initial picking.

Starting thresholds:

- `CTF fit resolution (A)`: a lenient general cutoff is about 6 A; clean high-resolution datasets often tolerate 3-5 A.
- `Relative Ice Thickness`: inspect the distribution and remove the high-thickness tail, not a copied number from another dataset.
- `Intrinsic Ice Defect Area %`: remove obvious defect-heavy micrographs when the junk detector identifies contaminated regions.

Use Micrograph Denoiser when particles are small, low contrast, in thick ice, on carbon/graphene, in detergent, or hard to threshold. Use Micrograph Junk Detector when contaminants or support features create high-confidence false picks.

### 2.3 Pick Particles Conservatively

For a new protein, start with low-bias picking unless a reliable data-derived template already exists.

Default sequence:

1. Blob Picker, Blob Picker Tuner, Manual Picker, Filament Tracer, or Topaz depending on target type and visibility.
2. Inspect Picks with NCC and power thresholds.
3. Extract a downsampled early stack.
4. Generate 2D classes.
5. Use clean 2D classes as templates for Template Picker.
6. Inspect template picks, remove duplicates, and re-extract.

Picking rules:

- Estimate particle diameter from model, mass, micrograph appearance, and visible micelle/nanodisc if it drives picking.
- Use about 10% margin around the expected diameter for blob picking.
- Avoid external model/map templates unless the target is well characterized and particles are visible by eye; otherwise template bias is a real risk.
- Preserve plausible rare views even if they are lower contrast.
- For defocus-variable data, use pick score and power score calibration so one threshold works across low- and high-defocus micrographs.
- For Topaz, choose downsampling so particle shape remains visible; do not downsample so far that the particle becomes only a dot.

Interpret Inspect Picks:

- Low NCC often removes empty ice.
- High power removes crystalline ice, carbon edges, aggregates, or strong contaminants.
- Low power removes very weak empty/thin ice picks.
- Always check high-contrast junk, empty ice, low-defocus micrographs, and high-defocus micrographs before accepting thresholds.

### 2.4 Extract For Fast Early Curation

Use a physical box large enough to contain the particle plus delocalized high-frequency signal, but Fourier-crop for speed during early cleanup. Never choose an extraction box from pixel count alone. Before creating the job, calculate and record the source micrograph pixel size, largest plausible particle extent, full physical field of view, box-to-particle ratio, Fourier-cropped pixel size, and resulting Nyquist limit. Use `csw.plan_particle_extraction(...)` for this mandatory preflight; `csw.extract_particles(...)` repeats the validation and records it in the job description.

Starting values:

- `Extraction box size (pix)`: physical box must be at least 2 times the largest plausible particle extent; 2.5 times is preferred for flexible particles or uncertain maximum extents.
- Use the longest observed or plausible conformation, including micelle, nanodisc, lipid, nucleic acid, or other density that must remain in the reconstruction. Do not use a compact-state dimension when supported extended states are larger.
- Increase physical box size for high-defocus or high-resolution data because CTF-delocalized signal can extend beyond the visible particle.
- `Fourier-crop to box size (pix)`: choose a smaller box that gives roughly 2-3 A/pix for early 2D/3D cleanup.
- Fourier cropping preserves the full physical field of view; it changes the output sampling and Nyquist limit. Confirm the cropped stack can support the intended resolution, and retain or re-extract an adequately sampled stack for final refinement.
- Re-extract at full useful box size before final refinement, polishing, local refinement, or ligand interpretation.
- `Save results in 16-bit floating point`: true.

If using very large boxes, check GPU memory limits and CryoSPARC maximum box-size guidance before launching many expensive jobs.

After the first extraction and 2D classification, render representative particles and all 2D classes and inspect them for density near the image boundary. Check compact, extended, end-on, rare, and unusual views. If meaningful density enters roughly the outer 10-15% of the box, re-extract before downstream reconstruction and treat descendants of the clipped extraction as invalid. Do not reject a particle solely because an earlier extraction clipped it.

### 2.5 Clean With 2D Classification

Use 2D classification to remove obvious junk and generate templates. Do not over-prune early.

Starting values:

- `Number of 2D classes`: 80-100 for smaller/cleaner stacks, about 200 for large standard stacks, about 400 when view or junk diversity is high.
- `Maximum resolution (A)`: about 12 A for early cleanup and template generation.
- `2D zero pad factor`: 1 when speed matters and high-resolution 2D detail is not the goal.
- `initial classification uncertainty factor`: low values around 1 can separate junk strongly; higher values around 2-10 can help separate views/species in cleaner stacks.
- Circular masks can help with drifting classes, overlapping particles, multiple top views, and orientation-biased targets.

Select classes by biology and view diversity:

- Reject empty ice, contaminants, aggregates, overlapping particles, broken particles, and clear non-target particles.
- Keep plausible top, side, and oblique views, even when some are blurry.
- For severe preferred orientation, consider skipping aggressive 2D class selection and relying more on 3D curation.

### 2.6 Generate Initial 3D References

Use Ab-Initio Reconstruction to produce initial references, then use Heterogeneous Refinement for more reliable full-stack 3D curation.

Starting route:

1. Run Ab-Initio Reconstruction with 2-6 classes, depending on how dirty or heterogeneous the stack looks.
2. Identify good target volumes and obvious junk volumes.
3. Run Heterogeneous Refinement on all relevant particles using good and junk references.
4. Iterate only while the good class fraction, map quality, or view diversity improves.

Practical choices:

- Use 20k-100k particles for fast initial references unless maps are unstable or too noisy.
- Use one good class plus several junk classes when the main question is target-vs-junk.
- Use `Force hard classification`: true for distinct good-vs-junk sorting.
- Turn off or compare hard classification when classes are subtle or collapse.
- In high-symmetry particles, enforce symmetry during ab-initio only when the symmetric shell/core is the target. If asymmetric internal density matters, test C1 and turn off `Enforce non-negativity` if flattened models appear.

Do not treat ab-initio class assignments as final truth. The useful output is often the reference volumes; Heterogeneous Refinement usually gives better full-stack sorting.

### 2.7 Repick And Re-extract

After clean 2D templates and initial 3D cleanup:

1. Template pick the curated exposures using data-derived 2D classes.
2. Inspect template picks and tune thresholds.
3. Remove duplicate particles.
4. Re-extract with improved centers and a full useful box.

Starting values:

- `Particle diameter (A)` in Template Picker: include visible micelle/nanodisc if that drives recognition.
- `Minimum separation distance (A)` in Remove Duplicate Particles: about 30% of longest particle dimension, or around 50 A for many small membrane proteins. Tune for crowding and shape.

### 2.8 Build A Consensus Map

Choose the refinement based on target behavior:

- Homogeneous Refinement: rigid soluble particles with uniform local resolution.
- Non-Uniform Refinement: membrane proteins, micelles/nanodiscs, flexible domains, local resolution variation, nearby particles, and low-SNR stacks.
- Helical Refinement: filaments after filament tracing, 2D helical class cleanup, asymmetric helical refinement, symmetry search, then symmetric helical refinement.

Common consensus settings:

- `Symmetry`: apply only when biologically true for the signal you want to reconstruct.
- Use C1 when asymmetric ligands, domains, cargo, or symmetry-breaking features matter.
- `Minimize over per-particle scale`: often helpful for consensus Non-Uniform Refinement, especially membrane proteins or mixed-quality stacks.
- `Dynamic mask start resolution (A)`: set to 1 A to effectively disable dynamic masking when a dynamic mask could erase weak, asymmetric, internal, or flexible density.
- For membrane proteins, Non-Uniform regularization can reduce micelle influence. A dynamic mask may not add benefit when Non-Uniform regularization already handles disordered density.

Run a test Homogeneous Refinement when Non-Uniform Refinement gives a surprisingly good map from an obviously mixed or dirty stack. Non-Uniform regularization, adaptive marginalization, and per-particle scale can hide non-matching particles well enough to produce a usable consensus map while masking the underlying heterogeneity.

### 2.9 Polish Only After A Good Reference Exists

Do not polish a bad particle stack. Improve particles and references first.

Typical order:

1. Consensus refinement.
2. Global CTF Refinement.
3. Local CTF Refinement.
4. Reference Based Motion Correction.
5. Re-refine consensus or focused maps.

Use Global CTF Refinement when the reference is strong enough and enough particles support higher-order terms. Fit anisotropic magnification, beam tilt, trefoil, tetrafoil, and spherical aberration only when data quality and particle counts justify it.

Use Local CTF Refinement when per-particle or local defocus variation is plausible. Use RBMC when per-particle motion limits high-resolution or local features.

For very high-resolution maps, test Ewald Sphere Correction only near the final stages. Reconstruct with both curvature signs using the same mask; one sign should improve if Ewald curvature is limiting resolution.

### 2.10 Validate, Sharpen, And Interpret

Never trust a single global FSC number.

Inspect:

- Sharpened and unsharpened maps.
- Half-map FSC and mask-corrected FSC.
- The actual FSC mask, especially `mask_fsc_auto`.
- Local resolution in the region of biological interest.
- cFSC, cFAR, SCF, and orientation distribution.
- Per-particle scale histogram.
- Guinier plot, noise model, real-space slices, Fourier slices, and projections.
- Secondary-structure features expected at the reported resolution.
- Ligand, cofactor, lipid, glycan, ion, disulfide, and stoichiometry chemistry.

Map appearance should match the claimed resolution. Around 3 A, you should expect clear helices, separated beta strands, many side chains, and often interpretable ligand/cofactor density in high-quality local regions. If the map reports high resolution but shows streaks, tubes, disconnected blobs, missing secondary structure, or fragmented ligand density, diagnose anisotropy, masking, over-sharpening, heterogeneity, or overfitting.

Use Sharpening Tools manually when auto-sharpening fragments flexible domains or ligands. Choose the B-factor that supports honest model building in the region being interpreted, not the one that makes the core look most dramatic.

## 3. Branching Playbooks

### 3.1 Membrane Proteins, GPCRs, Channels, And Micelles

Common risks:

- Low mass outside the micelle.
- Preferred top views.
- Micelle/nanodisc density dominates alignment.
- Flexible extracellular or intracellular domains.
- Ligands or peptides have partial occupancy.
- Local refinement overfits if masks include too much micelle or too little signal.

Recommended approach:

1. Use denoising and junk detection if particles are low contrast or contaminants are common.
2. Start with blob picking, then use data-derived 2D templates.
3. Avoid over-selecting 2D classes; rare side/oblique views may look weak.
4. Prefer Non-Uniform Refinement for consensus maps.
5. Inspect per-particle scale. A shoulder or second peak can reveal poor particles or alternate species.
6. Use local refinement only after consensus poses are strong.
7. Make masks loose, smooth, and biologically coherent. Do not tightly trace transmembrane helices.
8. Avoid subtracting micelles unless there is a compelling reason; subtraction can remove useful signal.
9. Use 3DVA and focused 3D Classification to explore flexible domains and ligand occupancy.

For local refinement:

- Use pose/shift gaussian priors.
- Start with rotation prior around 3-6 deg and shift prior around 2-3 A.
- Use an initial lowpass a few A coarser than the consensus, or coarser if only low-resolution signal is robust.
- Turn off per-particle scale for small masked local refinements unless testing shows it helps.

### 3.2 Ligand, Substrate, Cofactor, Or Drug Occupancy

The ligand question is usually a classification and validation problem, not just a sharpening problem.

Workflow:

1. Build the cleanest possible consensus particle stack first.
2. Refine CTF and motion before subtle ligand classification.
3. Design a full solvent mask covering the entire particle.
4. Design a focus mask around the binding site, including enough surrounding protein to stabilize classification.
5. Run 3D Classification from good poses.
6. Reconstruct hard classes with Heterogeneous Reconstruction Only or re-refine classes when poses may be biased.
7. Compare classes with apo/model difference maps.
8. Manually regroup classes by density shape and chemistry.
9. Tune sharpening for ligand interpretability.

Starting 3D Classification choices:

- `Number of classes`: 2 for obvious apo/bound, more for multiple poses or exploratory oversampling.
- `Filter resolution (A)`: coarsest resolution where the ligand/site feature remains visible. Ligand density may need 3.5-4 A; coarse domain changes may need 8-15 A.
- `Class similarity`: low, often 0-0.1, when density should differ independently.
- `Force hard classification`: useful for coarse distinct states; risky for subtle high-resolution differences.
- Use both solvent and focus masks. A focus mask alone can make classes mismatch the full particle projections.

Interpret ligand density with sample chemistry. Oxidation state, partial occupancy, ligand flexibility, cofactors, lipids, and local resolution can explain real density variation or reveal artifacts.

### 3.3 Discrete Heterogeneity

Use more than one sorting strategy when the biology matters.

Methods:

- Ab-Initio Reconstruction plus Heterogeneous Refinement: good for discovering unexpected species, large conformational differences, or when poses need updating.
- 3D Classification: good when poses are already strong and the state difference can be isolated with masks.
- Heterogeneous Refinement: good when classification should update poses.
- 3DVA Cluster mode: useful for particle subsets along continuous or mixed heterogeneity.
- Subset Particles by Statistic: useful when per-particle scale reveals quality/species peaks.

Compare strategies by:

- Per-class map interpretability.
- Reproducibility across replicate jobs.
- Particle-set intersections and unions.
- Known biology, stoichiometry, and state-specific features.
- Whether class populations are stable enough to interpret.

Do not overinterpret population percentages from a single classification job. Different methods can assign substantially different populations while producing similarly useful maps.

### 3.4 Continuous Heterogeneity, 3DVA, And 3D Flex

Use 3DVA first to understand what motion exists. Use 3D Flex only when a plausible continuous deformation and enough particles justify the complexity.

3DVA:

- Requires good upstream poses and a sensible mask.
- `Number of modes to solve`: start with 1-3 based on expected degrees of freedom.
- `Filter resolution (A)`: set to the scale of motion, not to the best map resolution.
- Use Display modes:
  - simple: component movies.
  - intermediates: maps along a local trajectory.
  - cluster: particle subsets for downstream refinement.
- If density appears/disappears, treat it as possible discrete compositional heterogeneity and test 3D Classification or Heterogeneous Refinement.

3D Flex:

- Crop before downsampling.
- Choose training pixel size so motion is above Nyquist.
- Build masks from the downsampled/cropped volume.
- Design a mesh that respects independently moving domains.
- Inspect latent distributions; useful particles should occupy a meaningful range rather than collapsing near zero or piling at bounds.
- Inspect validation losses; if rigid and full curves are too close and maps barely move, rigidity may be too high.
- If maps show nonphysical stretching or "jelly-like" helices, rigidity is too low or mesh segmentation is wrong.
- Use full-resolution 3D Flex Generate to judge secondary-structure plausibility.
- Cross-check 3D Flex states with 3D Classification or Heterogeneous Refinement when possible.

### 3.5 Preferred Orientation And Anisotropy

Preferred orientation can make a high global FSC meaningless.

Warning signs:

- 2D classes dominated by one view.
- Streaking, vertical tubes, flattened density, or missing secondary structure.
- cFSC varies strongly by direction.
- cFAR is poor.
- SCF suggests undersampled Fourier regions.
- Global resolution is much better than the visual map.

Responses:

1. Run Orientation Diagnostics.
2. Distinguish missing views from overrepresented views.
3. If views are absent, change picking, avoid over-pruning rare views, process tilted data if available, or collect tilt.
4. If rare views exist but are underrepresented, use Rebalance Orientations.
5. Use `alignments3D/alpha` as an intra-bin exclusion criterion to preferentially remove low-scale particles from overrepresented orientation bins.
6. Consider skipping aggressive 2D selection if it removes rare side/oblique views.

Rebalance Orientations cannot recover information that is not present in the particle stack.

### 3.6 Symmetry, Pseudosymmetry, And Symmetry Mismatch

Apply symmetry only to density that truly follows that symmetry.

Cases:

- True point symmetry: enforce symmetry for the symmetric signal.
- Pseudosymmetry: use symmetry relaxation rather than blindly enforcing symmetry.
- Symmetric core with asymmetric ligand/domain/cargo: use C1 or focused classification/local refinement to preserve the asymmetric feature.
- Local repeated units: use symmetry expansion, local masks, UID reassignment, and local refinement.
- High-symmetry shell with asymmetric cargo: refine shell and cargo through separate branches.

Symmetry relaxation:

- Set `Symmetry` to the pseudo-symmetry group.
- Set `Symmetry relaxation method` to maximization or marginalization.
- Use marginalization more often for smaller/lower-SNR targets.
- Add extra final passes because symmetry-breaking density may improve after global FSC convergence.
- Set `Maximum align resolution (A)` to the coarsest resolution where the symmetry-breaking feature remains visible.

For symmetry-related states, classify first, then align classes or rotate particle subsets into a common frame before final local refinement.

### 3.7 High-Symmetry Shells, Cargo, And Local Symmetry

High-symmetry particles can produce excellent shell maps while hiding weaker asymmetric or local-symmetry signal.

Workflow:

1. Build a robust shell/core refinement, often with symmetry.
2. Preserve C1 alignments if internal asymmetric contents matter.
3. Disable dynamic masking if it erases internal density.
4. Generate a shell map for Particle Subtraction.
5. Subtract the dominant shell/core.
6. Classify or align the remaining cargo.
7. For multiple local copies, make masks for each copy, re-center each local copy, reassign UIDs, align maps to superimpose copies, then locally refine with true local symmetry.

Do not refine all local copies at once unless they are already superimposed in a common frame.

### 3.8 Flexible Peripheral Domains And Local Refinement

Local refinement is a local search around good starting poses. It is not a replacement for a good consensus alignment.

Use local refinement when:

- Consensus map has a good core but a blurred domain.
- The region has enough signal to refine with priors.
- A biologically coherent mask can be designed.
- Upstream poses and shifts are reliable.

Avoid or revise when:

- The mask is tiny relative to the particle.
- The mask includes detergent, micelle, or independently moving density.
- Maps show radial plates, streaks, suspiciously high FSC, or implausible density.
- The region outside the mask dominates particle images. Use Particle Subtraction if necessary.

Key parameters:

- `Use pose/shift gaussian prior during alignment`: true.
- `Standard deviation (deg) of prior over rotation`: start around 3-6.
- `Standard deviation (A) of prior over shifts`: start around 2-3.
- `Re-center rotations each iteration?`: true for subregion refinement.
- `Re-center shifts each iteration?`: true for subregion refinement.
- `Initial lowpass resolution (A)`: a few A coarser than reliable consensus signal, or coarser for weak domains.
- Fulcrum coordinates can help domains that rotate around a known hinge.

### 3.9 Helical Proteins

For filaments:

1. Preprocess and curate exposures as usual.
2. Manually pick enough filaments to tune Filament Tracer.
3. Trace filaments and inspect picks.
4. Extract segments with appropriate box and inter-box distance.
5. Run 2D classification to remove bad segments and assess helical order.
6. Run asymmetric helical refinement.
7. Search helical symmetry.
8. Run symmetric helical refinement using the validated symmetry.

Do not borrow globular-particle assumptions for filaments. Segment spacing, helical symmetry, and filament quality drive the workflow.

### 3.10 Negative Stain, Phase Plate, And Special Acquisition Modes

Negative stain:

- Mark exposures as negative stain.
- Turn off CTF correction where appropriate.
- Use negative stain mainly for sample quality, oligomeric state, or rough low-resolution shape, not high-resolution model building.

Phase plate:

- Mark phase plate data during import or metadata preparation.
- Make sure downstream jobs use the correct phase-plate-aware CTF model.

AFIS/beam-shift data:

- Import or recover beam-shift metadata.
- Cluster exposure groups by beam shift.
- Use those exposure groups for CTF refinement.

EER:

- Import EER movies with the intended upsampling/fractionation strategy.
- Keep storage and downstream box-size choices in mind because EER can rapidly increase data volume.

## 4. Diagnostics Cheat Sheet

### Picking Plots

- NCC vs power: tune away empty ice, low-contrast false positives, and high-power junk.
- Pick count vs micrograph stats: reveals bad exposures or thresholds that fail at certain defocus/ice ranges.
- Defocus-calibrated pick scores: make thresholds more stable across defocus.

### 2D Classification

- Class averages: should show multiple views, consistent particle size, and recognizable features.
- Class ESS: very low ESS can indicate tiny or unstable classes.
- Probability of best class: poor separation can mean too many classes, low SNR, heterogeneous particles, or poor centering.
- Radial blur in small particles can be acceptable when marginalization accounts for pose uncertainty; streaky classes may indicate alignment problems.

### 3D Refinement

- Real-space slices/projections: identify missing density, flattening, streaks, or wrong topology.
- Fourier slices: reveal anisotropy and directional missing information.
- FSC: useful only with mask inspection and map inspection.
- cFSC/cFAR/SCF: diagnose directional resolution and orientation sampling.
- Per-particle scale: low, negative, shouldered, or bimodal distributions can reveal junk, poor contrast, wrong species, or nonmatching particles.
- Local resolution: confirms whether the region of interest supports model building.
- Guinier plot and noise model: diagnose sharpening, overfitting, CTF issues, or unusual noise behavior.

### FSC And Mask Issues

Warning signs:

- FSC does not drop to zero.
- FSC has sharp bumps consistent with CTF problems.
- Corrected FSC has a strong dip.
- `mask_fsc_auto` cuts through density or includes unwanted density.
- Reported global resolution is better than visible map features.

Responses:

- Inspect half maps and masks directly.
- Create a manual mask from a filtered map, segmentation, or model-derived molmap.
- Use enough dilation and soft padding.
- Compare corrected and uncorrected curves.
- Use local-resolution and visual map quality to judge the region being interpreted.

## 5. Common Failure Modes And Corrective Actions

| Failure mode | Likely cause | Corrective action |
| --- | --- | --- |
| Mostly junk picks | Bad picker thresholds, contaminants, wrong diameter | Use denoiser/junk detector, retune NCC/power, revise diameter, try Topaz or manual seeds |
| Good 2D classes but poor 3D map | Missing views, bad centers, mixed species, wrong symmetry | Run ab-initio with more classes, avoid aggressive 2D pruning, test C1, inspect orientation diagnostics |
| Ab-initio flattened high-symmetry model | Symmetry/initial model problem | Enforce true symmetry for shell, align to symmetry axes, or disable non-negativity when preserving asymmetric density |
| Heterogeneous classes collapse | Classes too subtle, masks wrong, filter too fine, hard classification wrong | Use solvent plus focus masks, lower-resolution filter, turn off hard classification, seed with better volumes |
| NU refinement looks good from dirty stack | NU regularization/scale/marginalization downweighted mismatches | Inspect per-particle scale, run homogeneous diagnostic, sort by scale or heterogeneity methods |
| High FSC but bad map | Anisotropy, bad mask, over-sharpening, heterogeneity | Run orientation diagnostics, inspect masks, manual FSC validation, revise sharpening, classify |
| Local refinement overfits | Mask too small/tight, poor poses, too broad search, micelle included | Add priors, loosen/smooth mask, lower align resolution, subtract outside density, improve stack first |
| Ligand density inconsistent | Partial occupancy, over-sharpening, wrong class, chemistry issue | Focused classification, regroup classes, compare apo/model difference maps, tune B-factor |
| 3DVA modes uninterpretable | Mask includes unrelated motion, too many modes, wrong filter | Redesign mask, reduce modes, set filter to motion scale |
| 3D Flex maps barely move | Rigidity or centering too high | Reduce rigidity and latent centering; inspect latent distribution |
| 3D Flex maps stretch helices | Rigidity too low or mesh wrong | Increase rigidity, segment mesh, revise rigid/fused regions, inspect full-resolution generate maps |
| Preferred orientation persists | Views absent or over-pruned | Re-pick to recover views, process tilt, avoid 2D over-cleaning, use rebalance only if views exist |

## 6. Suggested Parameter Starting Points

These are starting points, not rules.

| Stage | Parameter | Starting point |
| --- | --- | --- |
| Exposure curation | `CTF fit resolution (A)` | 6 A lenient; 3-5 A for clean high-resolution datasets |
| Picking | Diameter range | Expected diameter plus about 10% margin |
| Junk rejection | `Rejection distance from junk` | About 30 A; increase for large particles or diffuse junk |
| Extraction | Physical box | 1.5-2.5x particle diameter |
| Early extraction | Fourier-cropped pixel size | About 2-3 A/pix |
| 2D classification | Classes | 80-100 small/clean, 200 standard, 400 diverse/dirty |
| 2D classification | `Maximum resolution (A)` | About 12 A for early cleanup |
| 2D classification | Uncertainty factor | 1 for strong junk separation; 2-10 for broader view/species separation |
| Ab-initio | Particles | 20k-100k for fast initial references |
| Ab-initio | Classes | 2-6 for dirty stacks; good plus junk classes |
| Heterogeneous refinement | `Force hard classification` | True for distinct good/junk; test off for subtle states |
| Consensus NU | `Minimize over per-particle scale` | Often true for whole-particle consensus |
| Consensus/refinement | `Dynamic mask start resolution (A)` | 1 A to effectively disable when weak density must be preserved |
| Local refinement | Rotation prior | 3-6 deg |
| Local refinement | Shift prior | 2-3 A |
| 3D classification | `Class similarity` | 0-0.1 for independent density differences |
| 3D classification | `Filter resolution (A)` | Coarsest resolution where feature remains visible |
| 3DVA | Modes | Start with 1-3 |
| 3D Flex | Latent range | Aim for useful spread, roughly not collapsed near 0 or piled at bounds |

## 7. How To Decide What To Do Next

After each major job, ask:

1. Did particle count, view diversity, or map interpretability improve?
2. Did the job answer the hypothesis it was launched to test?
3. Are the diagnostics consistent with the visible map?
4. Did the branch remove rare but important signal?
5. Are class assignments reproducible enough to interpret?
6. Is the region of biological interest better, or only the global resolution number?
7. Would a simpler diagnostic job disprove the current interpretation?

Continue an iteration only if it improves a concrete diagnostic. Stop iterating when repeated cleanup no longer improves map quality, view diversity, or biological interpretability.

## 8. Minimal Reusable Pipeline Template

```text
1. Define target biology and expected processing risks.
2. Import movies/micrographs with correct metadata and exposure groups.
3. Patch Motion Correction, Patch CTF Estimation.
4. Curate exposures by CTF, ice, motion, intensity, junk, and obvious outliers.
5. Denoise and/or detect junk if picking is hard.
6. Blob/manual/Topaz/filament pick with conservative thresholds.
7. Extract downsampled particles for fast curation.
8. 2D Classification; keep real view diversity.
9. Ab-initio Reconstruction to make good/junk or state references.
10. Heterogeneous Refinement to clean particles in 3D.
11. Template pick with data-derived classes; remove duplicates.
12. Re-extract full-size particles.
13. Consensus Homogeneous or Non-Uniform Refinement with correct symmetry.
14. Diagnose orientation, per-particle scale, local resolution, FSC/masks, and map features.
15. Polish with Global CTF, Local CTF, and RBMC after a good reference exists.
16. Branch:
    - 3D Classification/Heterogeneous Refinement for discrete states.
    - 3DVA/3D Flex for continuous motion.
    - Local Refinement/Particle Subtraction for domains.
    - Symmetry relaxation/local symmetry expansion for symmetry mismatch.
    - Rebalance/re-pick/tilt for orientation bias.
17. Validate half maps, masks, local resolution, chemistry, and sharpening.
18. Record final particle provenance and the reason for every kept/discarded subset.
```

## 9. Documentation Discipline

Keep a processing log with:

- Dataset/import group and metadata assumptions.
- Exposure curation thresholds and number retained.
- Picking method, diameter, thresholds, and number of picks.
- Extraction box, crop size, pixel size, and particle counts.
- 2D classes kept/rejected and why.
- Ab-initio and heterogeneous references used.
- Refinement settings, symmetry, masks, and particle inputs.
- CTF/RBMC polishing sequence.
- Classification masks, filters, class counts, and replicate behavior.
- Final particle-set intersections/unions if using classification replicates.
- Final validation masks, sharpening B-factors, and local-resolution observations.

This log matters because the final map is only as interpretable as the decisions that produced the final particle stack.

## 10. Source Coverage Checklist

The guide above synthesizes the requested tutorial set, including:

- Discrete and continuous heterogeneity in FaNaC1.
- Discrete heterogeneity in acetogenin-bound complex I.
- DkTx-bound TRPV1.
- MAVS helical processing.
- Motor-bound nucleosome processing and the novel nucleosome state follow-up.
- Ligand-bound and inactive GPCR workflows.
- Encapsulated ferritin with shell subtraction and local symmetry.
- Exploratory data processing notes.
- Picking-induced orientation bias in HA trimer.
- Rapid EMPIAR-10291 processing.
- TRPV5/calmodulin pseudosymmetry.
- Yeast tri-snRNP local refinement and subtraction.
- Mask creation in Chimera/ChimeraX.
- Negative stain, phase plate, EER, EPU AFIS beam shift, float16, and performance notes.
- 3D Classification, 3DVA, 3D Flex, 3D Flex mesh preparation, and 3D Flex dependency notes.
- Blob Picker Tuner, particle picking calibration, common plots, CTF refinement, dynamic masking, Ewald sphere correction, patch motion/CTF, symmetry relaxation, BILD files, and membrane-protein tips.

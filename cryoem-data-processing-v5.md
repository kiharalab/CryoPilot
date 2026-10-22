# Cryo-EM Data Processing Guide (CryoSPARC v5)

This guide is a practical framework for single-particle cryo-EM processing in CryoSPARC v5.0.x. It distils the CryoSPARC job documentation, tutorials and case studies into decisions that transfer between targets. Job names and parameter names in backticks match the v5 user interface, so they can be searched in the live job register with `csw.job_spec(...)`. The numbers are starting points that were useful across many datasets, not values to copy blindly. Always confirm a parameter against the installed job spec before relying on it, because names and defaults change between releases.

The core rule is simple: start from the biology, then let diagnostics decide the processing branch. A rigid soluble enzyme, a small membrane protein in a micelle, a pseudosymmetric oligomer, a helical filament and a high-symmetry shell with asymmetric cargo fail in different ways and need different routes.

## 1. Start With A Processing Hypothesis

Before you create jobs, describe the target in processing terms:

- Expected molecular weight, particle diameter, longest plausible extent in any conformation, and whether micelle, nanodisc, lipid, support film or nucleic acid density should be visible.
- Domain layout: compact, elongated, hollow, membrane-embedded, flexible arms, peripheral domains, internal cargo, repeated local units, or helical assembly.
- Expected symmetry: C1, true point symmetry, local symmetry, pseudosymmetry, symmetry mismatch, or helical symmetry.
- Expected heterogeneity: junk and broken particles, other species in the sample, apo and bound states, partial occupancy, conformational states, continuous motion, or preferred orientation.
- Prior information: predicted or homologous models, published maps, ligand chemistry, stoichiometry, glycosylation, lipids and cofactors, and whether handedness is known.

Use this hypothesis to decide what each job is meant to test:

- "Are these picks real particles?" is tested by 2D classification and multi-class ab-initio reconstruction.
- "Is this a clean, homogeneous stack?" is tested by heterogeneous refinement, per-particle scale, 3D classification and 3D variability analysis.
- "Is the ligand really present?" is tested by focused 3D classification without scale optimization, re-refinement of classes, difference maps and chemical plausibility.
- "Is the resolution believable?" is tested by mask inspection, the corrected FSC, Orientation Diagnostics, local resolution and the visible features of the map.

## 2. Baseline Workflow

Use this route for a new single-particle dataset. Branch only when diagnostics or the biology justify it.

### 2.1 Import

1. Import movies with `Import Movies` (or `Import Micrographs` for data that is already motion-corrected). Set `Raw pixel size`, `Accelerating voltage`, `Spherical aberration`, `Total exposure dose`, and the gain reference. Check the gain orientation (for example `Flip gain ref & defect file in Y?`) on a few micrographs before you process everything.
2. Import collection sessions separately when the gain references differ in appearance, the frame counts differ, or the total dose differs by more than about 10%. Sessions with similar dose and gain can share one import job.
3. Reference Based Motion Correction requires an identical total dose for every exposure in a job. If sessions with different doses were combined, split them again with `Exposure Group Utilities` before RBMC.
4. Every import job gets its own exposure group ID. Use `Exposure Group Utilities` to inspect, split or combine groups before CTF refinement.
5. Negative stain: turn on `Negative Stain Data` at import, or set it later with `Exposure Tools`. For imported particle stacks use `Data Sign` (light-on-dark). To skip CTF correction, either turn on `Output Constant CTF` at import or turn off `Do CTF correction` in 2D Classification. Once the flag is set, pickers, extraction and Topaz handle the contrast sign automatically.
6. Phase plate: turn on `Phase Plate Data` at import, or set it with `Exposure Tools`. Patch CTF Estimation then searches and refines the phase shift automatically; no downstream parameter is needed.
7. EER movies: set `EER Number of Fractions` (default 40 is suitable in most cases; too many raises memory use and run time, too few weakens motion correction) and `EER Upsampling Factor` (1, 2 or 4). An upsampling factor of 2 doubles the pixel count in each direction and can recover a little information beyond physical Nyquist; 4 gives very large images and high GPU memory use.
8. EPU AFIS or beam-shift data: import the EPU XML metadata in the Import Movies "XML Import" section, using the filename prefix/suffix trimming parameters so each movie matches its XML. For data already imported, use `Import Beam Shift` connected to the full, uncurated exposure set. Check the event log for the beam-shift scatter plot and for warnings that most exposures have no beam shift.

### 2.2 Motion Correction And CTF Estimation

1. Run `Patch Motion Correction`. It replaces Full-frame and Local Motion Correction. Use `Only process this many movies` to test settings on a subset.
2. Turn on `Save results in 16-bit floating point` (off by default). It halves storage; processing still uses 32-bit arithmetic.
3. Turn on `Output denoiser training data` if you may use the Micrograph Denoiser. The denoiser needs these even/odd half-micrographs and a pixel size finer than about 3 Å.
4. Run `Patch CTF Estimation`. It handles tilted or bent ice, small particles and phase plate data. If you re-run Patch CTF after extraction, use `Patch CTF Extraction` to update the per-particle CTF values without re-extracting the images.
5. For AFIS data, cluster exposures into beam-shift groups after CTF estimation (see Section 3.10).

### 2.3 Curate Exposures Before Picking

Remove bad micrographs before you extract particles from them. In `Manually Curate Exposures`, inspect:

- CTF fit resolution.
- Relative ice thickness (remove the thick-ice tail of this dataset's distribution, not a value copied from another dataset).
- Total full-frame and local motion.
- Defocus and astigmatism outliers.
- Average intensity outliers.
- Junk area from the Micrograph Junk Detector.
- Particle count outliers after initial picking.

Starting values:

- `CTF fit resolution (A)`: about 6 Å is a lenient cutoff; clean high-resolution datasets often tolerate 3.5–5 Å.
- Thresholds can be set as parameters so the job runs without interaction (for example a `CTF fit resolution (A)` range of `0,6`). This is the preferred route for an automated agent.

Use the `Micrograph Denoiser` when particles are small, low contrast, in thick ice, on a support film, or in detergent. Use the `Micrograph Junk Detector` when ice crystals, ethane contamination, carbon or support edges cause false picks. Connect particles to the Junk Detector to reject picks near junk with `Rejection distance from junk (A)` (about 30 Å is a reasonable start; increase it for large particles or diffuse junk). In v5, `Min label area for particle rejection (A^2)` stops strongly contrasting particles from being labelled as junk.

### 2.4 Pick Particles Conservatively

For a new target, start with picking methods that have little template bias:

1. `Blob Picker`, `Blob Picker Tuner`, `Manual Picker`, `Filament Tracer` or Topaz, depending on the target and how visible it is.
2. `Inspect Particle Picks` with NCC and power thresholds.
3. Extract a Fourier-cropped early stack.
4. 2D classification.
5. `Template Picker` with clean, data-derived 2D classes.
6. Inspect the template picks, remove duplicates and re-extract.

Blob picking:

- Choose the blob shape with `Use circular, elliptical, or ring blob`. Elliptical blobs suit elongated particles: set the minimum diameter to the short axis and the maximum to the long axis. Ring blobs suit hollow particles.
- For circular blobs, keep the maximum diameter a little below the longest extent when full-length views are rare, so compact views are not missed.
- `Blob size spacing (A)` controls how many blob sizes are tried.
- To pick on denoised micrographs, turn on `Pick on denoised micrographs`. By default the pickers use the raw micrographs even when denoised ones exist.

Blob Picker Tuner:

- Provide roughly 15–40 manual picks (more helps for crowded or low-contrast data), placed on good micrographs that span the defocus range, including particles that sit close together.
- Set the particle agreement distance to about the particle size in Å; the job needs it to run correctly.
- If many parameter sets tie for best, label more particles.

Template picking:

- Use about 20–50 clean, diverse 2D classes as templates. More templates make picking slower without adding much.
- Set `Particle diameter in Angstrom` about 10–20% larger than the particle, and include a micelle or nanodisc if it dominates the particle's appearance.
- Reduce `Min. separation dist (diameters)` for crowded micrographs.
- Avoid templates from external maps or models unless particles are clearly visible and the target is well characterised, because template bias is real.

Inspect Particle Picks:

- `Calibrate Pick Score to CTF` and `Calibrate Power Score to CTF` are on by default, so one threshold works across low- and high-defocus micrographs. Confirm the threshold on micrographs at both ends of the defocus range.
- Low NCC usually removes empty ice. High power removes ice crystals, carbon edges, aggregates and strong contaminants.
- Be careful with the low-power cut: side views and other low-contrast views can have about half the contrast of top views and are the first to be lost.
- When picks come from denoised micrographs, the NCC and power scores are computed on the denoised images, but particles are still extracted from the raw micrographs. Thresholds do not transfer between raw and denoised runs.
- `Auto cluster` with a target power score can set thresholds automatically for denoised picks.
- For very dense template picks it is acceptable to skip Inspect Picks and curate in 3D instead.

For Topaz, choose the downsampling so the particle shape is still visible; do not downsample until the particle is a dot.

### 2.5 Extract For Fast Early Curation

The physical box must contain the whole particle plus its CTF-delocalised signal. Fourier cropping then makes early jobs fast without shrinking the field of view.

Before every extraction, run `csw.plan_particle_extraction(...)`. It records the source micrograph pixel size, the largest plausible particle extent, the physical field of view, the box-to-particle ratio, the Fourier-cropped pixel size and the resulting Nyquist limit. `csw.extract_particles(...)` repeats the check and writes it into the job description.

Box size:

- This toolkit requires a physical box of at least 2× the largest plausible particle extent and prefers 2.5× for flexible particles or uncertain extents. A smaller box needs `allow_small_box=True` and a written rationale. The CryoSPARC tutorials use 1.5–2.5× as a rough rule, so a smaller box can be justified, for example for a large rigid particle at very fine pixel size where 2× would exceed GPU memory.
- Use the longest plausible conformation, including micelle, nanodisc, lipid or nucleic acid density that must stay in the reconstruction.
- For final high-resolution refinement, 2–3× is typical. High defocus or very high resolution needs more room, because the delocalisation radius R = λΔF/d does not shrink with the particle (box ≈ D + 2R).
- Prefer box sizes with small prime factors (for example 256, 288, 320, 360, 384, 400, 432, 448, 512); they process faster.

Fourier cropping:

- Set `Fourier-crop to box size (pix)` so early 2D and 3D cleanup runs at roughly 2.5–5 Å/pix. A cropped box of about 64–128 px is common.
- The Nyquist limit is twice the cropped pixel size. Keep every early resolution limit coarser than it: the 2D `Maximum resolution (A)`, the ab-initio resolution, and Heterogeneous Refinement (set `Refinement box size (voxels)` to the cropped box).
- The CPU extraction job can write a second, smaller stack in the same run with `Second (small) F-crop box size (pix)`.
- Re-extract at full sampling before final refinement, CTF refinement, RBMC, local refinement or ligand interpretation. If a refinement FSC stays high up to Nyquist, the stack was cropped too far; re-extract with a larger crop box (new alignment is not needed).
- `Recenter using aligned shifts` is on by default; re-extraction after 3D alignment therefore re-centres particles.
- `Save results in 16-bit floating point`: on.

After the first extraction and 2D classification, render representative particles and all 2D classes and look for density near the box edge. Check compact, extended, end-on, rare and unusual views. If meaningful density enters roughly the outer 10–15% of the box, re-extract before downstream reconstruction and treat descendants of the clipped extraction as invalid. Do not reject a particle only because an earlier extraction clipped it.

For very large boxes, check GPU memory (Section 6.4) before launching many expensive jobs.

### 2.6 Clean With 2D Classification

Use 2D classification to remove obvious junk and to make templates. Do not over-prune early; 3D curation is usually more reliable.

Starting values:

- `Number of 2D Classes`: 50–200 is a good start for hundreds of thousands of particles. Use more when junk or view diversity is high, and fewer for small or clean stacks.
- `Maximum resolution (A)`: about 10–12 Å for early cleanup, and never finer than the Nyquist limit of the cropped stack.
- `Initial classification uncertainty factor`: 1–2 is robust. Values that are too high make all classes look alike.
- `Circular mask diameter (A)` and `Circular mask diameter outer (A)`: set them a little larger than the particle to keep classes centred, to free classes for rare views, and to stop neighbouring particles from driving the classification in crowded micrographs.
- `Minimum alignment res (A)`: 40–60 Å when a micelle, nanodisc or neighbouring density degrades alignment.
- For small or low-SNR particles, turn off `Force max over poses/shifts` so the job marginalises over poses and shifts. Classes look radially blurred but less streaky, and weak views are often recovered. It is slower. Increasing `Number of online-EM iterations` and doubling `Batchsize per class` (for example to 400) can also help.
- `Remove duplicate particles` is on by default for globular particles; turn it off for helical segments.

Reading the results:

- In v5, `Plotting sort method` defaults to similarity, so the plotted order is not the class index. Always select classes by index.
- The class ESS (effective number of assigned classes) is 1 for a confident assignment and approaches the number of classes when the assignment is uncertain. A high ESS means uncertain, overlapping or duplicate classes.
- Each class average shows a 0.5 FRC resolution; it is a guide to class quality, not a map resolution.
- Streaky or noisy classes suggest poor alignment; radial blur in small particles can be normal when marginalisation is on.

Selection rules:

- Reject empty ice, contaminants, aggregates, overlapping particles, broken particles and clearly non-target particles.
- Keep plausible top, side and oblique views, even when some are blurry or contain few particles.
- One round of 2D cleanup is often enough. For small particles, membrane proteins or severe preferred orientation, consider skipping 2D selection of the template-picked stack and curating entirely in 3D.
- `Reference Based Auto Select 2D` can select classes automatically against a reference volume.

### 2.7 Generate Initial 3D References

Use `Ab-Initio Reconstruction` to make references and `Heterogeneous Refinement` to sort the full stack.

Ab-initio:

- `Number of classes`: 2–6 for a good-plus-junk split. For discovering unexpected species, use many more classes (for example 10–15) on the whole stack, accepting a much longer run.
- `Number of particles to use`: leave it blank for multi-class runs. When it is set, only that subset is classified and passed to the outputs. A fixed subset (for example 20,000–100,000) is fine when you only need a reference volume.
- `Maximum resolution` and `Initial resolution`: small particles look like featureless blobs at the defaults. For small membrane proteins, finer values (for example about 9 Å initial and 7 Å maximum) can help.
- `Class similarity`: higher values spread particles evenly across classes early on and can improve discrimination between particle types; lower values (for example 0) let classes differ freely.
- `Enforce non-negativity`: turn it off if maps look flattened or when weak internal density matters.
- For octahedral, tetrahedral or icosahedral particles, impose symmetry to avoid a flattened model, and turn off `Center structures in real space` when internal asymmetric contents matter.
- v5 adds `Volume window mode` (spherical, cylindrical or none) with `Window diameter inner (A)` and `Window diameter outer (A)`.
- Use the "Unused particles" output when ab-initio was run on a subset.
- Ab-initio has a 50% chance of the wrong hand. Check handedness early (Section 2.12).

A fast junk reference: run ab-initio with 3 classes on the rejected 2D classes (or a small random subset), with a coarse maximum resolution (about 30 Å) and few iterations. If the rejected particles contain other proteins rather than ice and false picks, use more particles and the default settings.

Heterogeneous refinement:

1. Connect all relevant particles with one good volume and 1–5 junk volumes.
2. Use `Force hard classification` off in early rounds and on in later rounds, or whenever good and junk are clearly distinct.
3. Iterate until the good class holds roughly 90–95% of particles, a round removes only about 5% junk, or distinct views start splitting into separate classes. Check a class's orientation distribution before discarding it; top views can split off into their own class.
4. Make fresh junk volumes for each round. Junk that matches the previous junk volumes is already gone.
5. To split states or clean a good class, give Heterogeneous Refinement 2–6 identical copies of the same good volume with hard classification on. The copies diverge as the data dictate.
6. `Use spherical mask` with `Spherical mask diameter (A)` restricts the comparison to the particle.

Streaking along one direction in the good class often means junk is aligning to a single view.

`Homogeneous Ab-Initio Refinement` (BETA, v5) is a rescue route for stacks where 2D classes look good but global refinement fails. Follow it with Local Refinement using `Initialize from input half-maps`, or with Homogeneous Reconstruction Only.

### 2.8 Repick And Re-extract

After clean templates and 3D cleanup:

1. Template pick the curated exposures with data-derived 2D classes (an `Exposure Sets Tool` subset is enough to make templates).
2. Inspect the picks and tune thresholds, or curate them directly in 3D.
3. Remove duplicate particles with `Remove Duplicate Particles`.
4. Run duplicate removal again after 3D alignment, because the aligned shifts move particle centres.
5. Re-extract at full sampling with re-centring.

`Minimum separation distance (A)`: start at about 30–40% of the longest particle dimension and tune it for crowding and shape. Never remove duplicates from helical segments (Section 3.9).

### 2.9 Build A Consensus Map

Choose the refinement by target behaviour:

- `Homogeneous Refinement`: rigid soluble particles with uniform local resolution.
- `Non-uniform Refinement`: membrane proteins, flexible domains, variable local resolution, neighbouring particles and low-SNR stacks. It can also improve rigid but anisotropic maps.
- `Helical Refinement`: filaments (Section 3.9).

Key settings:

- `Symmetry`: apply it only when the signal you want to reconstruct is truly symmetric. Enforce it only once you are confident of the order.
- `Minimize over per-particle scale` is on by default in v5 refinements. Particles with zero or negative scale go to the new Rejected Particles output; inspect them.
- `Initial lowpass resolution (A)`: the default can be harsh for small targets; about 15 Å (or a few Å coarser than the reference's reliable resolution) often works better.
- `Adaptive Marginalization`: helps when pose certainty is low.
- Dynamic masking: in v5, turn off `Use dynamic refinement mask` to disable it (before v5, set `Dynamic mask start resolution (A)` to an unrealistically fine value such as 0.1 Å). Turn it off when a dynamic mask could erase weak, asymmetric, internal or flexible density. Non-uniform regularisation is less mask-dependent, and disabling dynamic masking can help it. A static input mask replaces dynamic masking for alignment only; the FSC is always computed with a dynamic resolution mask.
- In v5, `Dynamic mask threshold` (default 0.2), `Dynamic mask near` and `Dynamic mask far` (defaults 2.0 and 5.0) are multiples of the current resolution, not distances in Å. The mask therefore loosens automatically when the map is poor.
- For small, low-SNR particles where dynamic masking performs poorly, a soft static mask can help.

When dynamic masking is off, the refined volume carries a whole-box refinement mask. Downstream Orientation Diagnostics, Global and Local CTF Refinement, and RBMC then need `mask_fsc_auto` connected explicitly (in the volume's mask slot), or they use the whole box. 3D variability analysis needs its own generous mask from Volume Tools, otherwise box-corner noise dominates the modes.

Run a test Homogeneous Refinement when Non-uniform Refinement gives a surprisingly good map from a mixed stack. Non-uniform regularisation, adaptive marginalisation and per-particle scale can downweight non-matching particles and hide the heterogeneity underneath.

### 2.10 CTF Refinement, Motion Refinement And Ewald Correction

Do not polish a bad particle stack. Improve the particles and the reference first, and keep each step only if it helps.

Inputs: particles with 3D alignments and the volume, half-maps and mask from the same refinement.

Test each step against the same baseline with `Homogeneous Reconstruction Only` and the same fixed mask. This is a fair comparison because Non-uniform Refinement marginalises over poses. If several steps help, apply the one with the larger effect first.

Local CTF Refinement:

- Try it on every dataset that reaches about 4 Å or better; large, rigid particles benefit most, and small particles may get worse.
- Keep `Defocus search range` small after Patch CTF (about the expected ice thickness).
- Read the defocus landscapes: single deep wells mean a confident fit; broad or multiple wells mean the fit is uncertain. The defocus-change histogram should be sharply peaked, without heavy tails or pile-up at the ends of the search range.
- v5 adds `Optimize defocus over per-particle scale` (on by default).
- The CTF refinement tutorial runs Local CTF before Global CTF, because higher-order terms fit best once per-particle defocus is right.

Global CTF Refinement:

- Tilt and trefoil are fitted by default. Turn on spherical aberration and tetrafoil only once defocus and the third-order terms are well refined; fourth-order fitting can hurt otherwise.
- `Fit Anisotropic Magnification` needs `Number of iterations` of at least 2.
- Leave `Maximum fit res (A)` blank so it follows the FSC. For small particles, adjust `Minimum fit res (A)`.
- Keep a term only if its phase-error plot shows a clear data pattern and the residual is weaker than the data.
- Fit anisotropic magnification in the standalone Global CTF job, not iteratively inside a refinement, where it can become unstable.
- Run it on the largest, best-resolved reference available. CTF parameters belong to the microscope, not to a region of the map.
- Use exposure groups so each group gets its own aberrations.

On-the-fly CTF refinement: Homogeneous and Non-uniform Refinement can refine `Optimize per-particle defocus` and `Optimize per-group CTF params` during refinement. Confirm the benefit with the standalone jobs first.

Skip CTF refinement for very small subsets, and expect little benefit for small membrane proteins until the map is already detailed.

Re-extraction after CTF refinement: keep `Force re-extract CTFs from micrographs` off (the default), or the refined CTF values are replaced by the Patch CTF values.

Reference Based Motion Correction (RBMC):

- Needs exposures from Patch Motion Correction, a volume with half-maps and a mask, and particles from the same refinement (preferably with `Minimize over per-particle scale` on).
- All movies in a job need the same total dose and frame count (or turn on `Skip movies with wrong frame count`).
- Turn on `Save results in 16-bit floating point`. `Hyperparameter search thoroughness` set to Fast is usually enough. Set `In-memory cache size` to about 60–80% of RAM.
- It supports several reference volumes for heterogeneous data.
- Inspect the empirical dose weights. If late frames regain strong high-frequency weight, the fit may be overfitting; re-estimate with a stronger spatial prior.
- Typical gains are modest (a few tenths of an Å), but downstream local refinement often benefits more than the global number suggests. On small particles it can fail to help.
- Re-refine the RBMC particles afterwards. The dose weights and hyperparameters can be reused, including as an input to Patch Motion Correction for similar datasets.

Ewald sphere correction:

- Consider it only near the end, for maps better than about 3 Å from physically large particles, or for data collected at 200 kV or below. The DeRosier rule of thumb R = √(2·0.7/(tλ)) gives the spatial frequency at which curvature causes significant phase errors for a particle of diameter t and electron wavelength λ.
- Use `Homogeneous Reconstruction Only` with `Do EWS correction` and `EWS curvature sign`, run once with each sign and the same mask. The sign that improves the FSC is correct; find it once per dataset. If both signs give the same result, leave the correction off.
- Ewald correction combines with higher-order aberration correction; together they can outperform either one alone.

### 2.11 Validate, Sharpen And Interpret

Never trust a single global FSC number.

Inspect:

- Sharpened and unsharpened maps, real-space slices and projections.
- The refinement mask (`mask_refine`) and the resolution masks (`mask_fsc`, `mask_fsc_auto`) over the unmasked map, at threshold 1. Neither should cut into density or have a hard edge.
- The tight and corrected FSC curves (Section 4).
- Orientation Diagnostics (Section 3.5).
- The per-particle scale histogram and the Rejected Particles output.
- Local resolution in the region of biological interest.
- Secondary-structure features expected at the reported resolution, and ligand, cofactor, lipid, glycan, ion and disulfide chemistry.

Map appearance must match the claimed resolution. Around 3 Å you should see clear helices, separated β-strands and many side chains. If the map reports high resolution but shows streaks, tubes, disconnected blobs, spikes or shells, diagnose anisotropy, masking, over-sharpening, heterogeneity or overfitting.

Validation and reporting:

- Report resolution with `Validation (FSC)` and a custom, sufficiently loose mask (for example a model- or map-derived mask with a few pixels of dilation and a generous soft edge) when the automatic mask is too tight.
- `Local Resolution Estimation` needs a mask that contains all meaningful density; voxels outside it report meaningless values.
- `Sharpening Tools`: in v5 it uses `mask_fsc_auto` by default when no mask is connected, and offers `Which FSC to filter volume by` and a Butterworth lowpass (`Lowpass filter order`, `Lowpass filter corner resolution`) so several maps can be filtered identically for comparison. Choose the B-factor that supports honest model building in the region you interpret, not the one that makes the core look best. Ligand density often needs a milder B-factor than the core.

### 2.12 Handedness

Ab-initio maps have a 50% chance of the wrong hand, and the hand is easiest to fix early. Check it as soon as α-helices are resolved (they must be right-handed) or by fitting a model. If it is wrong, run `Volume Tools` with `Flip hand` and continue from the flipped volume. For helical refinements, flip the hand and also invert the twist.

## 3. Branching Playbooks

### 3.1 Membrane Proteins, GPCRs, Channels And Micelles

Common risks: little ordered mass outside the micelle, preferred views, micelle density dominating alignment, flexible soluble domains, partial occupancy of ligands or partners, and overfitting in local refinement.

Recommended approach:

1. Use the denoiser and junk detector when particles are low contrast or contaminants are common.
2. Start with blob picking, then pick with data-derived templates. Reduce the template picker's separation distance for crowded micrographs.
3. In 2D, set `Minimum alignment res (A)` to 40–60 Å and consider turning off `Force max over poses/shifts`. Avoid over-selecting 2D classes; rare side and oblique views may look weak. Curating in 3D is often better than 2D selection for small targets.
4. In ab-initio, use finer initial and maximum resolutions for small targets.
5. Prefer Non-uniform Refinement for consensus maps, with a finer `Initial lowpass resolution (A)` (about 15 Å) and a soft static mask if dynamic masking performs poorly.
6. Inspect the per-particle scale histogram. A shoulder or second peak can reveal poor particles or a second species; it can also reflect two ice-thickness populations.
7. Masks must be loose, smooth and biologically coherent. Do not trace transmembrane helices.
8. Avoid subtracting the micelle: it is disordered and hard to subtract without removing useful signal. Prefer local refinement with non-uniform regularisation and marginalisation. Subtracting a neighbouring particle or other rigid density outside a generous protein mask can help.
9. A dip in the FSC between about 9 and 5 Å is common for micelles and nanodiscs; it is not a fault by itself.
10. For 3D variability analysis, use a mask that excludes the micelle, and a high-pass resolution (about 20 Å) so modes do not capture low-frequency blobs.
11. Cryo-EM of small membrane proteins often shows a low cFAR even when the map is usable; check tFAR (Section 3.5).
12. Per-particle CTF refinement rarely helps small membrane targets until the map is already detailed.

"Spiky" density usually means junk remains in the stack. Purify further with multi-class ab-initio and heterogeneous refinement.

### 3.2 Ligand, Substrate, Cofactor Or Drug Occupancy

Ligand questions are classification and validation problems, not sharpening problems.

Workflow:

1. Build the cleanest consensus stack first, and refine CTF and motion before subtle classification.
2. Make a solvent mask covering the whole particle (in v5, `Generate solvent mask from consensus` makes one by default) and a focus mask around the binding site with enough surrounding protein to stabilise the classification.
3. Run `3D Classification` from good poses with `Per-particle scale` set to `none` (or `input` from a refinement with per-particle scale on). Optimising scale per class can hide whether density is present.
4. Use `Filter resolution (A)` of 3–6 Å for ligand presence or absence.
5. Reconstruct hard-assigned classes with `Heterogeneous Reconstruction Only`, then re-refine each class (Non-uniform Refinement with per-particle scale on). Inherited consensus poses are biased toward the dominant state; an apo class can look ligand-bound until it is re-refined.
6. Compare classes with difference maps from `Align 3D Maps` (the `map_difference` output) and with apo or model maps.
7. Regroup classes by density shape and chemistry, manually or with `Regroup 3D Classes`.
8. Tune sharpening for ligand interpretability.

Interpret ligand density together with the chemistry. Oxidation state, partial occupancy, flexibility, other cofactors, lipids and local resolution can explain real variation or reveal artefacts.

### 3.3 Discrete Heterogeneity

Use more than one sorting strategy when the biology matters.

Methods:

- Multi-class ab-initio plus heterogeneous refinement: discovers unexpected species and large differences, and updates poses.
- Heterogeneous refinement with identical copies of one volume: splits states without bias from the starting references.
- `3D Classification`: fast, alignment-free classification from good poses; the state difference can be isolated with masks. It tolerates many classes cheaply.
- 3D variability analysis in cluster mode: separates particles when the latent distribution is multimodal.
- `Subset Particles by Statistic`: splits on per-particle scale or other per-particle values.
- `Reference Based Auto Select 3D`: selects classes automatically by resolution or by correlation with a reference.

3D Classification parameters:

- `Number of classes`: 2 for an obvious apo/bound split; many more (40–80) to isolate states with low populations (below about 5%). Merge redundant classes afterwards with `Regroup 3D Classes`, which is designed for 50 or more classes.
- `Filter resolution (A)` (renamed from `Target resolution` in v4.5; it has no default and must be set): 3–6 Å for small density changes such as a ligand, 6–10 Å for domain rearrangements, and coarser than 10 Å for presence or absence of a domain or partner. Use the coarsest resolution at which the difference of interest is visible.
- `Init structure lowpass resolution (A)`: a few Å coarser than the filter resolution.
- Initialisation mode: PCA can give more reproducible starting volumes than simple mode for presence/absence questions; `input` seeds classes from supplied volumes (the number must match the class count).
- `Class similarity`: lower values (around 0.1) when classes should differ independently.
- `O-EM learning rate init` and `O-EM batch size (per class)`: if classes collapse during O-EM, reduce the learning rate in steps of 0.1 and/or reduce the batch size. Higher learning rates (up to about 1) let class volumes change quickly; low values (about 0.2) keep classes close to supplied input volumes. If the mean class ESS is not near 1 at the end of O-EM, increase `Number of O-EM epochs`.
- `Convergence criterion (%)` (default 2%) and `RMS density change convergence check` (on by default). For low-SNR particles the RMS check saves time.
- `Force hard classification`: consider it when many particles have a class ESS of 2 or more, or when classes smear for small targets. It works better with a coarse filter; with a fine filter it can collapse classes.
- `Use latent mixing coefficients` (v5, off by default): try it when the job returns many near-identical classes of equal size.
- `Per-particle scale`: the default `optimal` stops classes reshuffling; use `none` for presence/absence questions (Section 3.2). If reshuffling persists, classify from a refinement with per-particle scale on and use `input`.
- If class movies "wobble", correct anisotropic magnification with Global CTF Refinement and enable it in the classification.
- Class volumes are soft back-projections at the filter resolution, meant for inspection. Use `Heterogeneous Reconstruction Only` or re-refinement for real maps.

Masks for 3D Classification:

- A solvent mask alone behaves almost like no mask, and classes may collapse into one. Use a focus mask for the site of interest inside a generous solvent mask.
- In v5 the auto-generated solvent mask from the consensus always contains the focus mask.
- A focus mask can be designed from the difference between two 3DVA frames or two class maps.
- For sites related by pseudosymmetry, classify each site with a copy of the mask rotated by `Volume Alignment Tools`, rather than symmetry expanding, when the particle has intrinsic asymmetry.

Diagnostics: the class ESS histogram (near 1 is good; a mean around 3 with equal class sizes suggests continuous heterogeneity or too little signal in the mask), the class flow diagram and matrix, the difference-from-consensus slices, and the class assignment histogram.

Compare strategies by:

- Interpretability of each class map after re-refinement.
- Reproducibility across replicate jobs with different random seeds.
- Particle-set unions and intersections (`Particle Sets Tool`). For rare states, the union of replicates often gives a better map than one run, while the strict intersection discards too much.
- Known biology, stoichiometry and state-specific features.
- Whether populations are stable enough to interpret.

Do not over-interpret population percentages from one job. Validate a per-particle-scale subset against a random subset of the same size.

### 3.4 Continuous Heterogeneity, 3DVA And 3D Flex

Remove junk and grossly different species first (2D cleanup, multi-class ab-initio, heterogeneous refinement), then refine a consensus. 3D variability analysis (3DVA) is not meant for grossly different molecules. Use 3DVA to understand what motion exists; use 3D Flex only when a plausible continuous deformation and enough particles justify it.

Before 3DVA or 3D Flex on particles from several classes, put them in register with `Align 3D Maps` and `Update particle alignments` on; this takes minutes, not a full refinement.

3D Variability:

- Needs good upstream poses and a mask. Use a generous soft mask that covers where domains move to, not the whole box, and exclude micelles or nanodiscs.
- `Number of modes to solve`: default 3. Adding modes does not degrade the leading ones; one extra mode can absorb noise. Runtime grows linearly with modes.
- `Filter Resolution (A)`: coarser than the consensus resolution, typically 5–10 Å, and coarser for large domain motions.
- High-pass resolution: about 20 Å for small or membrane proteins, so modes do not capture low-frequency blobs.
- 3DVA cannot enforce symmetry. For symmetric particles, symmetry expand first.
- For screening, a 1-mode run per refined set can show whether a population is discrete (multimodal histogram) or continuous (single peak).

3D Variability Display (`Output mode`):

- `simple`: linear component movies. Good for small motions; it can break down for large motions and does not prove the frames exist in the data.
- `intermediates`: reconstructions from particle subsets in windows along one component (`Intermediates: window (frames)`: positive gives triangular weighting, 0 gives tophat, −1 gives bins with equal particle counts). Use it to show non-linear change and to output particle subsets along a component.
- `cluster`: Gaussian-mixture clustering in latent space, with a reconstruction per cluster. Use it when particles form clusters. Re-refine each cluster afterwards.
- `Downsample to box size`, `Crop to size (after downsample)` and `Only use these components` control output size and which modes are used.
- If density appears or disappears along a mode, treat it as discrete or compositional heterogeneity and test 3D Classification or heterogeneous refinement.

3D Flex (BETA): `3D Flex Data Prep` → `3D Flex Mesh Prep` → `3D Flex Training` → `3D Flex Generator` → `3D Flex Reconstruction`.

- 3D Flex cannot add or remove density. Separate compositional and discrete states before training. Side-chain and loop motions are too small to model, and latent distances are not energies.
- Data Prep crops, then downsamples (`Crop box size (pix)` then `Training box size (pix)`). The final images must be 440 px or smaller, because 3D Flex Reconstruction cannot use larger boxes. Choose the training pixel size so its Nyquist limit is on the scale of the motion; training does not use half-sets, so the training resolution must stay below the reconstruction resolution.
- Volume Tools resamples then crops, the opposite order to Data Prep. Match the two when making masks or full-resolution maps for 3D Flex jobs.
- The mesh mask defines the mesh boundary only; it does not need a soft edge. The solvent mask must not cut through density, or the reconstruction streaks.
- Mesh: `Base num. tetra cells` (finer meshes capture more detail but regularise less and can overfit small particles). For custom meshes, segment the map (ChimeraX Segger `.seg`, or an `.mrc` of segment IDs 0..S−1 with −1 for solvent), set fusions with `Segment connections` (parent>child pairs; try several orders), and mark segments rigid sparingly with `Rigid segments`, because rigidity propagates through fused vertices. A micelle is often best treated as a rigid segment.
- Training: start with 2 latent dimensions (`Number of latent dims`). `Rigidity (lambda)` is the main capacity control (default 2.0); tune it in large steps first, then small ones. `Latent centering strength` (default 20) keeps latents roughly within ±1.5 and does not change model capacity. `Number of hidden units` around 32 limits overfitting for simple motions. `Freeze latents during training` is for latents initialised from 3DVA.
- Inspect the latent distributions: useful particles span a meaningful range rather than collapsing near zero or piling up at the bounds.
- If generated maps barely move, reduce rigidity or increase capacity (latent dims, hidden units, finer mesh). If maps stretch helices or look jelly-like, increase rigidity or revise the mesh segmentation.
- Generator: use an odd `Number of frames per series`. To judge secondary-structure plausibility at full resolution, crop the full-resolution map to the Data Prep crop box and connect it to the Generator.
- 3D Flex Reconstruction: increase `Max BFGS iterations` (default 20) to about 40 if the FSC does not fall to zero or looks artefactual. GPU memory needs at least twice the full-resolution box volume.
- Latent spaces from different training runs cannot be compared directly.
- Cross-check 3D Flex states with 3D Classification seeded from Generator frames (`input` initialisation, low learning rate, hard classification) or with heterogeneous refinement.

### 3.5 Preferred Orientation And Anisotropy

Preferred orientation can make a good global FSC meaningless.

Warning signs: 2D classes dominated by one view, streaks or flattened density along one direction, missing secondary structure, and a global resolution much better than the visible map. The viewing-direction distribution plot alone does not reliably reveal preferred orientation; use the posterior precision plot and Orientation Diagnostics.

`Orientation Diagnostics`:

- Set `Symmetry` to match the upstream refinement. Connect particles to get SCF*. Connect a volumes group to diagnose every class.
- cFAR below 0.5 indicates anisotropy that harms the map; above 0.5 the map is generally isotropic enough to build into.
- SCF* below 0.81 indicates preferred orientation in the viewing directions (it ignores signal content, so junk counts like real particles).
- Read the two together:

| cFAR | SCF* | Conclusion |
| --- | --- | --- |
| high | high | No orientation bias |
| high | low | Inconclusive; perhaps a pathological alignment distribution |
| low | high | Anisotropy from junk or misalignment, not missing views |
| low | low | Junk and/or preferred orientation |

- tFAR (v5): small membrane proteins and targets with one or two dominant views can have a pathologically low cFAR while the map is usable. Check tFAR in those cases.
- Relative signal by viewing direction shows which views are under-represented.
- A low cFSC along a conical axis does not by itself mean more particles are needed from that direction.
- cFAR can stay flat while both the best and worst cFSC change; always inspect the map.

Responses:

1. If cFAR is low and SCF* is high, treat it as an alignment or junk problem: clean in 3D, try Non-uniform Refinement, turn off masking, or subtract neighbouring density.
2. Distinguish missing views from under-represented views using relative signal and 2D classes.
3. If views are missing: change picking (elliptical blobs, a smaller maximum diameter, no low-power cut), avoid over-pruning in 2D, curate in 3D only, process tilted data if available, or collect tilted data.
4. If rare views exist but are under-represented, use `Rebalance Orientations`, then re-refine (a reconstruction alone is not enough). Turn on `Minimize over per-particle scale` and consider `Adaptive Marginalization`.
5. Use the more isotropic rebalanced map to make templates (`Create Templates`) and re-pick with Template Picker or Topaz to recover rare views.

`Rebalance Orientations`:

- It needs 3D poses and works on orientation bins, not particles. `Rebalance percentile` trims bins above that percentile down to its count; test several values (for example 80, 60, 40). Too aggressive a cut fragments the map.
- `Number of orientation bins`: the default is usually fine; more bins target a narrow over-represented view.
- `Intra-bin exclusion criterion`: `Random`, `alignments3D/alpha` (lowest per-particle scale first), `alignments3D/error`, `alignments2D/error` or `pick_stats/ncc_score`. Scale and error depend on the reference and can be unreliable when the map is highly anisotropic, so compare criteria and prefer `Random` when the reference is poor.
- It cannot recover information that is not in the particle stack.

### 3.6 Symmetry, Pseudosymmetry And Symmetry Mismatch

Apply symmetry only to density that truly follows it.

Cases:

- True point symmetry: enforce it for the symmetric signal.
- Pseudosymmetry: use symmetry relaxation instead of enforcing symmetry.
- Symmetric core with an asymmetric ligand, domain or cargo: use C1, symmetry relaxation, or focused classification plus local refinement.
- Repeated local units: symmetry expansion, local masks and local refinement (Section 3.7).
- High-symmetry shell with asymmetric cargo: refine the shell and the cargo in separate branches.

Symmetry relaxation (Homogeneous and Non-uniform Refinement only):

1. Get a reference on the symmetry axes: run ab-initio with the symmetry enforced, or use `Volume Alignment Tools` with `Do symmetry alignment` and the matching symmetry.
2. Optionally make an asymmetric starting reference with `Homogeneous Reconstruction Only` and `Break Symmetry` on.
3. Refine with `Symmetry` set to the pseudo-symmetry group and `Symmetry relaxation method` set to `maximization` or `marginalization`. Maximization may be enough for large, high-SNR particles; use marginalization for smaller particles, smaller masks and lower SNR.
4. Set `Number of extra final passes` well above 0 (5–30). The GSFSC often plateaus while the symmetry-breaking density is still improving. Set a high value and stop the job manually, because a finished job cannot be continued.
5. Set `Maximum align resolution (A)` to the coarsest resolution at which the symmetry-breaking feature is still visible (around 6–10 Å is common).
6. Finish with a C1 Local Refinement rather than another global refinement, so particles cannot flip back to an equivalent pose.

Judge the result by inspecting the map, not by the FSC.

Classification route for symmetry-related states: classify with the number of classes equal to the symmetry order, a solvent mask over the whole particle and a focus mask over all candidate sites; then use `Align 3D Maps` with `Update particle alignments` to bring classes into a common frame, then run a C1 Local Refinement. When more classes than real states are used, regroup them afterwards; the true number of states needs independent evidence.

For symmetry-mismatched features (for example linkers or partners that break the core symmetry), classify in C1 with a coarse filter and hard classification, rotate classes or particle subsets into a common frame with `Volume Alignment Tools`, re-classify with `input` initialisation, and locally refine in C1.

### 3.7 High-Symmetry Shells, Cargo And Local Symmetry

High-symmetry particles can produce excellent shell maps while hiding weaker asymmetric or locally symmetric contents.

Workflow:

1. Ab-initio with symmetry, `Center structures in real space` off and `Enforce non-negativity` off when internal contents matter.
2. Refine the shell with symmetry relaxation rather than enforced symmetry, and with dynamic masking off, so particles keep relaxed (effectively C1) poses and internal density is not erased.
3. For a symmetric shell reference, run `Homogeneous Reconstruction Only` with the symmetry on the relaxed particles. This keeps the C1 poses; a symmetric re-refinement would destroy the symmetry break.
4. Run Global CTF Refinement on the shell, because it is the largest, best-resolved signal.
5. Make a shell map and mask for `Particle Subtraction`, then subtract the shell.
6. Classify or align the remaining cargo.
7. Group re-alignment of cargo: run 3D Classification with fixed poses, many classes, hard classification, a high O-EM learning rate with no decay, and a soft spherical solvent mask (so classes are not biased toward the consensus orientation). Then use `Align 3D Maps` with `Update particle alignments`. Repeat the cycle once or twice before local refinement. 3D Classification tolerates junk better than alignment-based jobs and cannot overfit through poses.
8. For several local copies: make a mask for each copy, re-centre each copy (`Volume Alignment Tools`, `Re-center to mask center of mass`, with UID reassignment so copies are distinct particles), align the copies into a common frame, then refine with the true local symmetry.
9. Before Local Refinement with symmetry, align the map to the symmetry axes with `Volume Alignment Tools` and `Do symmetry alignment`; local refinement applies symmetry about the box centre.

Do not refine all local copies together unless they are already superimposed in a common frame.

### 3.8 Flexible Peripheral Domains, Local Refinement And Subtraction

Local refinement is a local search around good starting poses; it does not replace a good consensus alignment.

Use local refinement when the consensus has a good core but a blurred domain, the region has enough signal, a biologically coherent mask can be made, and upstream poses are reliable. Avoid or revise it when the mask is tiny relative to the particle, includes detergent or independently moving density, or the maps show radial plates, streaks, shells or blips at the mask edge, or a suspiciously high FSC.

Key parameters (`Local Refinement`):

- `Use pose/shift Gaussian prior during alignment`: on for small regions, small masks or low SNR; its value depends on how good the starting poses are. Start with prior widths of about 3–6° and 2–3 Å.
- `Rotation search extent (deg)` and `Shift search extent (A)`: use tighter extents for small masks. If angle or shift changes pile up at the limit in late iterations, increase the extent.
- `Re-center rotations each iteration?` and `Re-center shifts each iteration?`: on for subregion refinement.
- Fulcrum: by default the mask centre. Set the hinge point between the domain and the body with the fulcrum override (`x,y,z`, with a `px` or `A` suffix), for example from a ChimeraX marker or `measure center`.
- `Initial lowpass resolution (A)`: a few Å coarser than the reliable consensus resolution, or near the resolution where the consensus FSC is still about 1. The default can be too harsh for small regions.
- `Maximum align resolution (A)`: limit it for small regions (for example around 3–5 Å) to prevent overfitting, and use `Number of extra final passes` of 0.
- Marginalization and non-uniform regularisation are on by default; keep them on for micelles and disordered regions.
- `Minimize over per-particle scale`: off for small masked subregions; on when the mask covers the whole complex.
- `Initialize from input half-maps`: use it after Homogeneous Ab-Initio Refinement or when the input half-maps are independent.

Masks for local refinement: build them from the consensus map or a model-derived map, binarise, dilate a few pixels and add a generous soft edge. Small domains need relatively larger masks. Over-dilation pulls in rigid core density and inflates the resolution.

Particle subtraction:

- Helps mostly when a large rigid part dominates a smaller region, and particle alignments are good enough to model the large part accurately.
- The subtraction mask covers what you remove (the complement of the region you keep). Do not dilate it; give it a soft edge of at least 5 × (resolution ÷ pixel size) pixels, and build it from the well-aligned consensus.
- Check the subtracted particles with a quick `Homogeneous Reconstruction Only`.
- Subtraction can improve mid-frequency FSC and local regions even when the global resolution barely changes.

For weak, off-centre domains: place a marker on the domain, re-centre with `Volume Alignment Tools` (`3D Coordinates of new center (voxels)`), and re-extract with a larger box. The domain often improves even if the reported global resolution gets worse.

### 3.9 Helical Proteins

Do not borrow globular-particle assumptions for filaments.

1. Preprocess and curate exposures as usual.
2. Manually pick about 150–200 filament segments on at least 10 micrographs that span the defocus range, avoiding crossings and edges. Run 2D Classification with few classes and `Force max over poses/shifts` on to make templates. `Filament Tracer` can also run template-free with minimum and maximum filament diameters.
3. Trace filaments with `Filament Tracer`. Set the filament diameter and the separation distance between segments (in filament diameters; about 0.25 is a common start).
4. Inspect picks with the extra filament sliders (local power, curvature and sinuosity) in `Inspect Particle Picks`; choose thresholds from this dataset's distributions.
5. Extract segments with a box that holds several helical repeats.
6. 2D Classification with `Align filament classes vertically` on and `Remove duplicate particles` off. Never run Remove Duplicate Particles on dense segment picks; Helical Refinement keeps neighbouring segments in the same half-set. Feeding good classes back into the tracer as templates can improve picking.
7. Run `Helical Refinement` without symmetry first. A cylinder is generated if no volume is supplied; use ab-initio for filaments that are not cylinder-like.
8. Search for the helical symmetry with `Symmetry Search Utility` (asymmetric volume plus mask, a range of pitches and subunits per turn, both hands). Use `Average Power Spectra` to check layer lines.
9. Run symmetric Helical Refinement with the candidate twist and rise. Symmetry optimisation begins once the GSFSC is better than about 5 Å.
10. Check handedness with resolved α-helices. If the hand is wrong, flip the map with `Volume Tools` (`Flip hand`) and refine again with the inverted twist and the same rise.
11. Turn on `Use Non-Uniform Refinement` in Helical Refinement for the final map.

### 3.10 Exposure Groups And Beam Shift

Exposure groups let CTF refinement fit aberrations per group.

- Inspect groups with `Exposure Group Utilities` (`Action`: `info_only`).
- Split groups by a filename token with `split` (`Field to use to split Dataset`, `Start Slice Index`, `Number of characters to Consider`), or set and combine them with `combine&set`.
- For beam-shift (AFIS) data, run `Exposure Group Utilities` after CTF estimation with `Input Selection` set to exposures and `Action` set to `cluster&split`. Use `agglomerative` clustering for ring patterns and `kmeans` for isotropic or continuous spread. Aim for even exposure counts and beam-shift ranges per cluster. Too few clusters leave large variation inside groups; too many leave too few particles per group to fit aberrations stably. Exposures without beam-shift values form one extra group.
- When particles already exist, connect both exposures and particles and turn on `Correspond particles to exposures and enforce consistency of exposure group IDs`. Use the `take_mode` combine strategy if particles were already split across groups.
- Validate by comparing Global CTF Refinement with and without groups, using `Homogeneous Reconstruction Only` with a fixed mask. Gains are usually modest on a well-calibrated microscope.

## 4. Diagnostics Cheat Sheet

### Picking Plots

- NCC versus power: separates empty ice, low-contrast false positives and high-power junk.
- Pick count against micrograph statistics: reveals bad exposures or thresholds that fail at some defocus or ice thicknesses.
- Defocus-calibrated scores are on by default; check the calibration plots in the job log.

### 2D Classification

- Class averages should show several views, a consistent particle size and recognisable features.
- Class ESS: 1 means a confident assignment; values near the number of classes mean uncertainty. A high median ESS points to duplicate or overly similar classes, or an incomplete classification.
- Probability of best class: poor separation can mean too many classes, low SNR, mixed particles or poor centring.
- Radial blur in small particles is acceptable with marginalisation; streaky classes suggest alignment problems.

### 3D Refinement

- Real-space slices and projections: missing density, flattening, streaks, spikes, shells or wrong topology.
- Masks: inspect the refinement and resolution masks over the unmasked map.
- Per-particle scale histogram: low, negative, shouldered or bimodal distributions can reveal junk, poor contrast, another species or ice-thickness populations. Check the Rejected Particles output.
- Posterior precision directional distribution: if the minimum and maximum are within about an order of magnitude, orientation sampling is adequate.
- Orientation Diagnostics: cFAR, SCF*, tFAR and relative signal (Section 3.5).
- Local resolution: confirms whether the region of interest supports model building.
- Guinier plot (fit from 10 Å to the 0.143 resolution) and noise model: a noise model that rises at high frequency is normal, because of dose weighting.

### FSC And Mask Issues

Normal behaviour:

- The corrected FSC often shows a sharp dip at the phase-randomisation resolution. This is a sign that phase randomisation worked.
- A dip between about 9 and 5 Å is common for micelles and nanodiscs.
- A dip in the corrected FSC is expected in local refinement.

Warning signs:

- The corrected curve stays well below the tight curve over a wide range: the auto-tightened mask is too tight. Make a looser custom mask and re-validate with `Validation (FSC)`.
- A sudden drop rather than a smooth dip: density outside the mask.
- The FSC stays high up to Nyquist: the stack was cropped too far (re-extract larger), or duplicate particles remain (a long tail; run Remove Duplicate Particles).
- Oscillations or bumps: check pixel size, voltage and spherical aberration.
- The reported resolution is better than the visible map features.

Responses:

- Inspect half-maps and masks directly.
- Make a manual mask from a filtered map, a segmentation or a model-derived map (Section 5).
- Use enough dilation and soft padding.
- Compare corrected and tight curves; the corrected FSC does not reliably detect overfitting by itself.
- Judge the region being interpreted by local resolution and visual quality.

## 5. Masks

Masks must be smooth: no sharp step from 1 to 0, which causes ringing and can introduce artefactual signal common to both half-maps. Err on the side of loose but smooth masks, especially for small proteins; avoid masks shaped like the secondary structure.

Building masks:

- Start from a lowpass-filtered or Gaussian-blurred map (for example ChimeraX `volume gaussian #1 sDev 2`), a segmentation, or a model-derived map. For model-derived maps use `molmap` at 12 Å or coarser with `onGrid` the target map; without `onGrid` the mask is unusable.
- Put segmentations back on the map grid with `volume resample #N onGrid #1` before importing.
- Import the mask base as a volume and finish it in `Volume Tools`: lowpass, threshold, binarise, dilate a few pixels, and add a soft edge. Volume Tools lowpasses before thresholding, so pick the threshold on the filtered map.
- Soft padding: at least 5 × (GSFSC resolution ÷ pixel size) pixels. Dilation and padding are in pixels of the input map, so a downsampled map needs fewer pixels. Start with a soft edge that is too wide and narrow it later.
- Use `Invert mask` for subtraction masks that keep everything except a region.
- Rotate or move a mask to an equivalent site with `Volume Alignment Tools`.
- 3D Flex mesh masks need no soft edge.

In ChimeraX, open the mask over the map at threshold 1 to check what it contains. Viewing-direction `[bild]` files can be opened over the map in ChimeraX to show the orientation distribution in 3D.

## 6. Parameter Starting Points And Limits

These are starting points, not rules.

### 6.1 Preprocessing And Picking

| Stage | Parameter | Starting point |
| --- | --- | --- |
| Import | `EER Number of Fractions` | 40 |
| Import | `EER Upsampling Factor` | 1 or 2; 4 only with ample GPU memory |
| Motion and extraction | `Save results in 16-bit floating point` | On |
| Exposure curation | `CTF fit resolution (A)` | 6 Å lenient; 3.5–5 Å for clean high-resolution data |
| Junk detection | `Rejection distance from junk (A)` | About 30 Å; larger for large particles or diffuse junk |
| Template picking | `Particle diameter in Angstrom` | About 10–20% larger than the particle |
| Blob Picker Tuner | Manual picks | 15–40, more for crowded or low-contrast data |
| Duplicates | `Minimum separation distance (A)` | 30–40% of the longest dimension; never for helical segments |

### 6.2 Extraction, 2D And Initial 3D

| Stage | Parameter | Starting point |
| --- | --- | --- |
| Extraction | Physical box | ≥ 2× largest extent (toolkit check); 2.5× for flexible particles; 2–3× for final refinement |
| Early extraction | Fourier-cropped pixel size | About 2.5–5 Å/pix |
| 2D classification | `Number of 2D Classes` | 50–200 |
| 2D classification | `Maximum resolution (A)` | 10–12 Å, coarser than cropped Nyquist |
| 2D classification | `Initial classification uncertainty factor` | 1–2 |
| 2D classification | `Minimum alignment res (A)` | 40–60 Å with micelles or neighbours |
| Ab-initio | `Number of particles to use` | Blank for multi-class runs |
| Ab-initio | `Number of classes` | 2–6 for good/junk; 10+ for species discovery |
| Heterogeneous refinement | `Force hard classification` | Off early, on later or for distinct good/junk |
| Heterogeneous refinement | `Refinement box size (voxels)` | The cropped box size |

### 6.3 Refinement And Heterogeneity

| Stage | Parameter | Starting point |
| --- | --- | --- |
| Refinement | `Minimize over per-particle scale` | On (v5 default) |
| Refinement | `Use dynamic refinement mask` | Off when weak or internal density must be preserved |
| Refinement | `Initial lowpass resolution (A)` | About 15 Å for small targets |
| Symmetry relaxation | `Number of extra final passes` | 5–30 |
| Global CTF | `Number of iterations` | ≥ 2 with anisotropic magnification |
| Local refinement | Prior widths | About 3–6° and 2–3 Å |
| Local refinement | `Maximum align resolution (A)` | About 3–5 Å for small regions |
| 3D Classification | `Filter resolution (A)` | 3–6 Å ligand; 6–10 Å domain motion; >10 Å presence of a domain |
| 3D Classification | `Number of classes` | 2 for obvious states; 40–80 for rare states, then regroup |
| 3D Classification | `Per-particle scale` | `none` for presence/absence questions |
| 3DVA | `Number of modes to solve` | 3 (default) |
| 3DVA | `Filter Resolution (A)` | 5–10 Å, coarser than the consensus |
| 3DVA | High-pass resolution | About 20 Å for small or membrane proteins |
| 3D Flex | `Number of latent dims` | 2 |
| 3D Flex | `Rigidity (lambda)` | 2.0 (default), tune by inspection |
| 3D Flex | Training images | ≤ 440 px |
| 3D Flex Reconstruction | `Max BFGS iterations` | 20; 40 if the FSC looks artefactual |

### 6.4 GPU Memory And Box Size

Approximate largest volume box for Homogeneous, Helical and Local Refinement with non-uniform regularisation off:

| GPU memory | Largest box (px) |
| --- | --- |
| 4 GB | ~680 |
| 8 GB | ~870 |
| 11 GB | ~975 |
| 12 GB | ~1000 |
| 16 GB | ~1110 |
| 24 GB or more | ~1125 |

Non-uniform regularisation needs more memory. If a job runs out of memory, lower `GPU batch size of images`, run `Downsample Particles`, or turn on `Low-Memory Mode` in Non-uniform Refinement (slower, but it handles larger boxes on smaller GPUs).

## 7. Common Failure Modes And Corrective Actions

| Failure mode | Likely cause | Corrective action |
| --- | --- | --- |
| Mostly junk picks | Bad thresholds, contaminants, wrong diameter | Denoiser and junk detector, retune NCC/power, revise the diameter or blob shape, Blob Picker Tuner, Topaz |
| Good 2D classes but poor 3D map | Missing views, bad centres, mixed species, wrong symmetry | Multi-class ab-initio, 3D-only curation, test C1, Orientation Diagnostics, Homogeneous Ab-Initio Refinement |
| Ab-initio flattened model | Symmetry or initialisation problem | Enforce true symmetry for high-symmetry shells; turn off non-negativity |
| Wrong handedness | 50% chance in ab-initio | Volume Tools `Flip hand`; invert the twist for helical data |
| Heterogeneous or 3D classes collapse | Mask, filter or learning rate | Solvent plus focus mask, coarser filter, lower learning rate or batch size, hard classification off with fine filters, identical-copy seeding |
| Many identical classes of equal size | Too little signal in the mask or continuous heterogeneity | Larger or better focus mask, `Use latent mixing coefficients`, fewer classes, 3DVA |
| Non-uniform map looks good from a dirty stack | Regularisation and scale downweight mismatches | Inspect per-particle scale and rejected particles, run a Homogeneous Refinement check, sort in 3D |
| High FSC but poor map | Anisotropy, bad mask, over-sharpening, heterogeneity | Orientation Diagnostics, inspect masks, `Validation (FSC)` with a custom mask, revise sharpening, classify |
| Low cFAR with high SCF* | Junk or misalignment | 3D cleanup, Non-uniform Refinement, no mask, subtract neighbouring density |
| Local refinement overfits | Mask too small or tight, poor poses, broad search | Priors, tighter search extents, limit `Maximum align resolution (A)`, looser and smoother mask, subtract outside density |
| Ligand density inconsistent | Partial occupancy, scale optimisation, biased poses | Focused classification with `Per-particle scale` `none`, re-refine classes, difference maps, tune B-factor |
| 3DVA modes uninterpretable | Mask covers micelle or box corners, wrong filter | Generous protein-only mask, high-pass ~20 Å, filter 5–10 Å |
| 3D Flex maps barely move | Rigidity too high or capacity too low | Lower rigidity; more latent dims, hidden units or a finer mesh |
| 3D Flex maps stretch helices | Rigidity too low or mesh wrong | Raise rigidity, segment the mesh, revise rigid and fused regions |
| Preferred orientation persists | Views absent or over-pruned | Re-pick, curate in 3D, Rebalance Orientations and re-refine, templates from the rebalanced map, tilted data |
| FSC stays high to Nyquist | Over-cropped stack or duplicates | Re-extract larger; Remove Duplicate Particles |
| Refined CTF lost after re-extraction | `Force re-extract CTFs from micrographs` on | Re-extract with it off |

## 8. How To Decide What To Do Next

After each major job, ask:

1. Did particle count, view diversity or map interpretability improve?
2. Did the job answer the hypothesis it was launched to test?
3. Are the diagnostics consistent with the visible map?
4. Did the branch remove rare but important signal?
5. Are class assignments reproducible enough to interpret?
6. Is the region of biological interest better, or only the global resolution number?
7. Would a simpler diagnostic job disprove the current interpretation?

Continue an iteration only if it improves a concrete diagnostic. Stop when repeated cleanup no longer improves map quality, view diversity or biological interpretability.

Explore on small, clean subsets first, then apply the strategy to the full dataset and reuse the volumes as seeds. A first pass can identify the best micrographs (most particles surviving cleanup), which can then train Topaz to re-pick everything. No single strategy is correct; combine approaches and compare them.

## 9. Minimal Reusable Pipeline Template

```text
1. Define target biology and expected processing risks.
2. Import movies with correct metadata; split by gain, dose and frame count; set
   negative stain, phase plate, EER and beam-shift options.
3. Patch Motion Correction (float16, denoiser training data) and Patch CTF Estimation.
4. Curate exposures by CTF fit, ice, motion, intensity, junk area and outliers.
5. Denoise and/or detect junk if picking is hard.
6. Blob, Blob Picker Tuner, manual, Topaz or filament picking with conservative
   thresholds.
7. Plan and extract a Fourier-cropped stack (box >= 2x the largest extent).
8. 2D Classification; keep real view diversity; check for clipping.
9. Multi-class Ab-Initio for good, junk or state references; check handedness.
10. Heterogeneous Refinement to clean particles in 3D; fresh junk volumes each round.
11. Template pick with data-derived classes; remove duplicates before and after
    3D alignment.
12. Re-extract at full sampling.
13. Consensus Homogeneous or Non-uniform Refinement with correct symmetry and an
    explicit masking choice.
14. Diagnose orientation, per-particle scale, rejected particles, local
    resolution, FSC and masks, and map features.
15. Test Local CTF, Global CTF and RBMC; keep only what helps.
16. Branch:
    - 3D Classification or Heterogeneous Refinement for discrete states, then
      re-refine classes.
    - 3DVA or 3D Flex for continuous motion.
    - Local Refinement and Particle Subtraction for domains.
    - Symmetry relaxation or local symmetry for symmetry mismatch.
    - Rebalance, re-pick or tilt for orientation bias.
17. Validate with Validation (FSC), masks, local resolution, chemistry and
    sharpening.
18. Record final particle provenance and the reason for every kept or discarded
    subset.
```

## 10. Documentation Discipline

Keep a processing log with:

- Import groups, metadata assumptions and exposure groups.
- Exposure curation thresholds and the number retained.
- Picking method, diameter, thresholds and number of picks.
- Extraction plan: box, crop size, pixel size, ratio, Nyquist and particle counts.
- 2D classes kept and rejected, and why.
- Ab-initio and heterogeneous references used.
- Refinement settings, symmetry, masking choice, masks and particle inputs.
- CTF and RBMC tests, with the comparison that justified each kept step.
- Classification masks, filters, class counts, per-particle scale mode and replicate behaviour.
- Final particle-set intersections and unions when classification replicates are used.
- Final validation masks, sharpening B-factors and local-resolution observations.

This log matters because the final map is only as interpretable as the decisions that produced the final particle stack.

## 11. Sources

This guide is based on the CryoSPARC v5.0 documentation at guide.cryosparc.com, read in October 2026: the job pages for every job named above, the tutorials on dynamic masking (v5), CTF refinement, Ewald sphere correction, symmetry relaxation, Orientation Diagnostics, mask creation, 3D Classification, 3D Variability Analysis, 3D Flex and mesh preparation, picking calibration, Blob Picker Tuner, EER, float16, negative stain, phase plate and beam-shift import, the membrane-protein tips and common-plots pages, and the end-to-end and heterogeneity case studies, plus the release notes from v4.4 to v5.0.7. Numbers taken from individual case studies were generalised into ranges, and dataset-specific values were left out.

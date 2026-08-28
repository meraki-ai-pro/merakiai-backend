# Level 100 Learn-Mode Animation Video Plan

## Purpose

This backlog turns the reviewed Calculus and Statistics Learn-mode sources into short concept videos. It is intentionally limited to ideas where motion materially improves understanding; administrative slides, reference lists, static formula sheets, and long marking schemes remain text-first resources.

The recommended format is 60–150 seconds per concept, with narration, captions, a final takeaway, and an optional one-question knowledge check. A video must explain one learning objective rather than replay an entire lecture deck.

## Rendering approach

- **Manim**: changing graphs, geometry, equations, probability distributions, limits, derivatives, and integrals.
- **Remotion**: data stories, tables becoming charts, real-world scenarios, labels, UI-style sequences, and branded narration/captions.
- **Hybrid**: Manim-generated mathematical scenes assembled and narrated in Remotion.

## Priority definitions

- **P0**: highest visual-learning value; create and validate first.
- **P1**: strong follow-up concept; create after P0 analytics and learner feedback.
- **P2**: useful worked example or reinforcement; generate on demand or in a later batch.

## Calculus backlog

| ID | Priority | Video concept | Learning objective and animation beats | Source anchor | Renderer | Difficulty |
|---|---|---|---|---|---|---|
| CAL-01 | P0 | From secant to tangent | Move a second point toward a fixed point, shrink `Δx`, and show average slope converging to instantaneous slope and the derivative. | `derivative_June2026.pptx` — rate of change, secant/tangent, derivative definition | Manim | Basic |
| CAL-02 | P0 | One-sided limits and continuity | Approach a point from left and right; contrast a continuous point, removable hole, jump, and vertical asymptote. | `derivative_June2026.pptx` — limits and continuity | Manim | Basic |
| CAL-03 | P1 | A derivative across an entire curve | Sweep a tangent along a curve while a second graph traces its slope; connect positive, zero, and negative derivative values to shape. | `derivative_June2026.pptx`; `application_of_Derivative_June2026.pptx` | Manim | Basic |
| CAL-04 | P1 | Product, quotient, and chain rules | Show two changing factors, a ratio, and nested function machines; reveal why the chain rule multiplies inner and outer rates. | `differentiatin_Techniques_June2026.pptx` | Hybrid | Intermediate |
| CAL-05 | P0 | Related rates: airplane and observer | Animate the changing right triangle, label known and unknown rates, differentiate the constraint, and solve at one instant. | `application_of_Derivative_June2026.pptx` — related-rates example | Hybrid | Intermediate |
| CAL-06 | P0 | Reading graph behavior from derivatives | Move across a curve and highlight increasing/decreasing intervals, critical points, local extrema, concavity, and inflection points. | `application_of_Derivative_June2026.pptx` | Manim | Intermediate |
| CAL-07 | P1 | Antiderivatives and the constant family | Start with a derivative graph, reconstruct vertically shifted antiderivatives, and explain why all answers include `+ C`. | `antiderrivative_June2026.pptx` | Manim | Basic |
| CAL-08 | P1 | Falling rock: position, velocity, acceleration | Animate the rock while the three related functions update; integrate acceleration and use initial conditions to select constants. | `antiderrivative_June2026.pptx` — motion example | Hybrid | Intermediate |
| CAL-09 | P0 | Area, accumulation, and the Fundamental Theorem | Grow an accumulated-area function as the upper limit moves, then show that its instantaneous change equals the curve height. | `integrationTechniques_June2026.pptx`; `application_antiderivative_June2026.pptx` | Manim | Intermediate |
| CAL-10 | P0 | Riemann sums converge to area | Compare left, right, and midpoint rectangles; increase subdivisions from coarse to fine and display error shrinking. | `riemannSum_June2026.pptx` | Manim | Basic |
| CAL-11 | P1 | Signed area and displacement | Let velocity cross the time axis; accumulate positive and negative regions and distinguish total displacement from distance travelled. | `riemannSum_June2026.pptx`; `application_antiderivative_June2026.pptx` | Manim | Intermediate |
| CAL-12 | P2 | Exponential growth, decay, and investment | Animate continuously compounded growth and decay curves while parameters change and connect the differential rate to the accumulated result. | `application_antiderivative_June2026.pptx` | Hybrid | Intermediate |
| CAL-13 | P2 | Energy consumption and battery depletion | Turn a time-varying power curve into accumulated energy used and remaining battery, with units visible throughout. | `application_antiderivative_June2026.pptx` | Remotion | Intermediate |

## Statistics backlog

| ID | Priority | Video concept | Learning objective and animation beats | Source anchor | Renderer | Difficulty |
|---|---|---|---|---|---|---|
| STA-01 | P0 | Population, sample, and repeated sampling | Select repeated samples from a population, compute each mean, and assemble the sampling distribution. | `Sampling_25_06_2026.pptx` | Hybrid | Basic |
| STA-02 | P0 | Central Limit Theorem | Morph non-normal population samples into distributions of sample means; increase `n` and show the shape normalize and standard error shrink. | `Sampling_25_06_2026.pptx` | Manim | Intermediate |
| STA-03 | P1 | Sampling methods and bias | Animate simple random, systematic, stratified, and cluster selection from the same population, then show biased coverage. | `DataDefinition&Collection_25_06_2026.pptx`; `Sampling_25_06_2026.pptx` | Remotion | Basic |
| STA-04 | P0 | Probability as events in a sample space | Build sample spaces and Venn regions; animate union, intersection, complement, mutually exclusive events, and the addition rule. | `ElementaryProbability_25_06_2026.pptx` | Hybrid | Basic |
| STA-05 | P1 | Permutations versus combinations | Fill ordered slots, then remove ordering to show why arrangements and selections use different counts. | `Counting&Probability_25_06_2026.pptx` | Hybrid | Basic |
| STA-06 | P1 | The birthday paradox | Add people one at a time and show the probability of at least one shared birthday rise much faster than intuition expects. | `Counting&Probability_25_06_2026.pptx` | Remotion | Intermediate |
| STA-07 | P0 | Bayes theorem as probability flow | Animate prior probabilities through evidence branches and recombine the highlighted paths into the posterior probability. | `BayesProbability_25_06_2026.pptx` | Hybrid | Intermediate |
| STA-08 | P1 | Expected value as a balance point | Build a discrete probability distribution, place its probability masses on a number line, and show the weighted balance point. | `Distributions_25_06_2026.pptx` | Manim | Basic |
| STA-09 | P1 | Binomial versus Poisson models | Contrast fixed trials/successes with arrivals in time or space; animate the assumptions before displaying either formula. | `Distributions_25_06_2026.pptx` | Hybrid | Intermediate |
| STA-10 | P0 | Understanding the normal curve | Vary `μ` and `σ`, keep total area at one, and show how center and spread affect the curve. | `Distributions_25_06_2026.pptx` | Manim | Basic |
| STA-11 | P0 | Z-scores and areas under the curve | Transform `X` to `Z`; move cutoffs and shade left-tail, right-tail, and between-area probabilities. | `Distributions_25_06_2026.pptx` | Manim | Intermediate |
| STA-12 | P0 | What 95% confidence really means | Draw many samples and intervals around a fixed population mean; preserve the intervals that cover it and highlight the few that miss. | `Estimations_25_06_2026.pptx` | Hybrid | Intermediate |
| STA-13 | P1 | Margin of error trade-offs | Adjust confidence level, variability, and sample size and animate the resulting interval width. | `Estimations_25_06_2026.pptx` | Manim | Intermediate |
| STA-14 | P0 | From raw data to a useful chart | Transform observations into ordered data, frequency tables, histogram/ogive/scatter plot, and select the suitable chart for the question. | `DataOrganization&Visualization_25_06_2026.pptx` | Remotion | Basic |
| STA-15 | P1 | How charts can mislead | Compare honest and truncated axes, distorted proportions, chartjunk, and inappropriate chart types using the same data. | `DataOrganization&Visualization_25_06_2026.pptx` | Remotion | Basic |
| STA-16 | P1 | Mean, median, and outliers | Add an extreme value and show the mean moving while the median remains more resistant; relate each measure to distribution shape. | `DataOrganization&Visualization_25_06_2026.pptx` | Hybrid | Basic |
| STA-17 | P1 | Variance and standard deviation | Draw deviations from the mean, square and average them, and compare distributions with equal centers but different spread. | `DataOrganization&Visualization_25_06_2026.pptx` | Manim | Basic |
| STA-18 | P2 | Quartiles, boxplots, skew, and outliers | Assemble a five-number summary into a boxplot, then move observations to reveal skew and flagged outliers. | `DataOrganization&Visualization_25_06_2026.pptx` | Hybrid | Basic |
| STA-19 | P2 | Correlation is not causation | Morph scatterplots from `r=-1` to `r=+1`, compare linear strength, and end with a confounding-variable example. | `DataOrganization&Visualization_25_06_2026.pptx`; `Inferences_25_06_2026.pptx` | Remotion | Intermediate |
| STA-20 | P2 | The DCOVA data workflow | Follow one Level 100 study through Define, Collect, Organize, Visualize, and Analyze, preserving the same question and data. | `DataDefinition&Collection_25_06_2026.pptx` | Remotion | Basic |

## Worked-example micro-videos

These should be short guided solutions linked from Learn or Review mode, rather than broad concept videos.

| ID | Priority | Worked example | Source | Renderer |
|---|---|---|---|---|
| EX-01 | P0 | Fraud detection posterior probability | `BayesProbability_25_06_2026.pptx` | Hybrid |
| EX-02 | P1 | Server-type failure posterior probability | `BayesProbability_25_06_2026.pptx` | Hybrid |
| EX-03 | P1 | Critical bugs in a fixed test batch | `Distributions_25_06_2026.pptx` | Hybrid |
| EX-04 | P1 | Server uptime as a binomial probability | `Distributions_25_06_2026.pptx` | Hybrid |
| EX-05 | P1 | Comparing scores with z-scores and finding a top-5% cutoff | `Distributions_25_06_2026.pptx` | Manim |
| EX-06 | P2 | Normal scores with the 68–95–99.7 rule | `Distributions_25_06_2026.pptx` | Manim |

## First production batch

The first batch should cover six independent paths through the production renderer and player:

1. `CAL-01` — Manim graph/geometry.
2. `CAL-10` — Manim rectangle convergence.
3. `STA-04` — hybrid sets/probability.
4. `STA-07` — hybrid probability tree.
5. `STA-12` — hybrid repeated-sampling story.
6. `STA-14` — Remotion data transformation.

This batch deliberately exercises graph drawing, equation labels, many moving objects, narration timing, captions, and the frontend video player. Approval criteria are mathematical accuracy, readable mobile-safe text, synchronized narration/captions, no clipped equations, successful replay/seeking, and a useful text fallback if rendering fails.

## Content and source-quality notes

- The Learn corpus reviewed here is Level 100 and is suitable for basic-to-intermediate explanations. Animation difficulty in this document means conceptual difficulty for the learner, not rendering complexity.
- `DataOrganization&Visualization_25_06_2026.pptx` contains a broken reference to an embedded workbook (`/ppt/embeddings/oleObject1.bin`). Its slide text was extracted, but the source deck should be repaired before it is treated as a visual design reference.
- The Statistics marking-scheme and tutorial Word documents were fully extracted for text and equations. Visual rendering was unavailable in the current local environment, so they are used only as worked-example sources, not as layout references.
- Do not generate one long video from the marking scheme. Each approved solution should become a separate micro-lesson with its own objective and answer check.

## Production acceptance gate

Before a generated animation is shown to students, require:

1. a source citation and matching course/topic metadata;
2. lecturer or reviewer approval for mathematics and narration;
3. a successful media probe for duration, codecs, audio, and dimensions;
4. captions and a transcript, with a text-answer fallback;
5. mobile and desktop player checks, including replay, seek, mute, and error states;
6. render timeout and retry limits, with no model-generated executable code;
7. analytics for render success/failure, render duration, playback start, and completion.

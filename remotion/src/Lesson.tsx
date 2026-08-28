import React from 'react';
import {
  AbsoluteFill,
  Sequence,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from 'remotion';
import type { ChartSpec, LessonSpec, Slide, Step } from './types';
import { FPS, STEP_SECONDS } from './types';

/**
 * One composition renders every archetype.
 *
 * Separate compositions per archetype would mean the Python side has to pick a
 * composition id, and a mismatch between the two lists becomes a render that
 * fails at the last step. One entry point, branching on spec.archetype, keeps
 * that agreement in a single place.
 */

const BG = '#0b1220';
const FG = '#f8fafc';
const MUTED = '#94a3b8';

const font =
  'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif';

function useFade(startFrame: number, durationFrames: number) {
  const frame = useCurrentFrame();
  const local = frame - startFrame;
  // Fade in over 12 frames, out over the last 10. Long enough to read as
  // deliberate, short enough not to eat a three-second beat.
  const opacity = interpolate(
    local,
    [0, 12, durationFrames - 10, durationFrames],
    [0, 1, 1, 0],
    { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' },
  );
  return Math.max(0, Math.min(1, opacity));
}

const TitleCard: React.FC<{ spec: LessonSpec }> = ({ spec }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const rise = spring({ frame, fps, config: { damping: 200 } });

  return (
    <AbsoluteFill
      style={{
        backgroundColor: BG,
        justifyContent: 'center',
        alignItems: 'center',
        padding: 80,
        fontFamily: font,
      }}
    >
      <div style={{ transform: `translateY(${interpolate(rise, [0, 1], [30, 0])}px)`, opacity: rise }}>
        <div style={{ width: 72, height: 5, backgroundColor: spec.accent, marginBottom: 32 }} />
        <h1 style={{ color: FG, fontSize: 68, lineHeight: 1.1, margin: 0, fontWeight: 700 }}>
          {spec.title}
        </h1>
        {spec.subtitle ? (
          <p style={{ color: MUTED, fontSize: 30, marginTop: 24, maxWidth: 1100 }}>
            {spec.subtitle}
          </p>
        ) : null}
      </div>
    </AbsoluteFill>
  );
};

const SlideCard: React.FC<{ slide: Slide; accent: string; start: number; length: number }> = ({
  slide,
  accent,
  start,
  length,
}) => {
  const opacity = useFade(start, length);
  return (
    <AbsoluteFill
      style={{
        backgroundColor: BG,
        padding: 90,
        justifyContent: 'center',
        fontFamily: font,
        opacity,
      }}
    >
      <div style={{ width: 56, height: 4, backgroundColor: accent, marginBottom: 28 }} />
      <h2 style={{ color: FG, fontSize: 52, margin: 0, lineHeight: 1.15, fontWeight: 650 }}>
        {slide.title}
      </h2>
      {slide.body ? (
        <p style={{ color: MUTED, fontSize: 30, lineHeight: 1.5, marginTop: 28, maxWidth: 1200 }}>
          {slide.body}
        </p>
      ) : null}
    </AbsoluteFill>
  );
};

/**
 * Fitting a process flow into the frame.
 *
 * Every step stays on screen once revealed — the point of a process flow is
 * seeing the whole chain at the end — so the column is laid out at its FULL
 * height from the first frame. Centre a column taller than the frame and step
 * one is pushed off the top while the not-yet-revealed last step holds blank
 * space at the bottom, so a student never sees where the process starts.
 *
 * Shrinking to fit is only half an answer: eight maximal steps scale down to
 * roughly 0.46, which is unreadable on a projector. Below MIN_SCALE the scene
 * PAGINATES instead — each page holds as many steps as fit at a readable size
 * and stays up for its own steps' beats.
 *
 * Heights are estimated rather than measured: Remotion lays out every frame
 * afresh, so reading back element heights would cost a second pass per frame.
 */
const CHARS_PER_DETAIL_LINE = 78;

// Below this, text stops being readable from the back of a lecture hall.
//
// 0.65, not 0.75. A five-step flow with ordinary detail solves to ~0.735 —
// entirely readable — and a 0.75 floor paginated it 4+1, so the closing frame
// showed step five alone and the chain the video exists to show was gone. The
// floor has to sit below the scale a normal flow lands on, or pagination fires
// on exactly the cases that did not need it.
const MIN_SCALE = 0.65;

const BASE_PADDING = 90;

/** Detail lines at a given scale. Smaller text fits MORE characters per line,
 *  which is why the scale cannot be solved in one pass. */
function detailLines(step: Step, scale: number): number {
  if (!step.detail) return 0;
  return Math.max(1, Math.ceil((step.detail.length * scale) / CHARS_PER_DETAIL_LINE));
}

function stepHeight(step: Step, scale: number): number {
  const lines = detailLines(step, scale);
  const textHeight = 45 + (lines ? 8 + lines * 34 : 0);
  return (Math.max(52, textHeight) + 26) * scale;
}

function totalHeight(steps: Step[], scale: number): number {
  return steps.reduce((sum, step) => sum + stepHeight(step, scale), 0);
}

/** Scale that fits these steps, found by iteration — line count depends on the
 *  scale, and the scale depends on the line count. Three passes converge. */
function fitScale(steps: Step[], available: number): number {
  let scale = 1;
  for (let i = 0; i < 3; i += 1) {
    const needed = totalHeight(steps, scale) / scale; // height at scale 1
    scale = Math.min(1, available / Math.max(needed, 1));
  }
  return scale;
}

/** Split into pages that each fit at MIN_SCALE. One page whenever they all do.
 *
 * Pages are BALANCED, not filled greedily. Greedy packing puts as much as
 * possible on page one and leaves the remainder alone on the last — six steps
 * became five and then one, so the video ended on a single orphaned line.
 * Even pages read better and end on a fuller frame.
 */
function paginate(steps: Step[], available: number): Step[][] {
  if (fitScale(steps, available) >= MIN_SCALE) return [steps];

  const fits = (chunks: Step[][]) =>
    chunks.every(
      (chunk) =>
        chunk.reduce((sum, step) => sum + stepHeight(step, MIN_SCALE), 0) <= available
    );

  // Grow the page count until every page fits, splitting as evenly as the
  // count allows.
  for (let count = 2; count <= steps.length; count += 1) {
    const perPage = Math.ceil(steps.length / count);
    const chunks: Step[][] = [];
    for (let i = 0; i < steps.length; i += perPage) {
      chunks.push(steps.slice(i, i + perPage));
    }
    if (fits(chunks)) return chunks;
  }

  // One step per page always fits — a single step taller than the frame is a
  // spec problem, not a pagination one.
  return steps.map((step) => [step]);
}

const StepsScene: React.FC<{ steps: Step[]; accent: string; startFrame: number }> = ({
  steps,
  accent,
  startFrame,
}) => {
  const frame = useCurrentFrame();
  const { height } = useVideoConfig();
  const perStep = STEP_SECONDS * FPS;

  const available = height - BASE_PADDING * 2;
  const pages = paginate(steps, available);

  // Which page the playhead is on. Steps keep their global beat, so the video
  // is the same length however it is paginated.
  const elapsed = Math.max(0, frame - startFrame);
  const currentStep = Math.min(steps.length - 1, Math.floor(elapsed / perStep));
  let pageIndex = 0;
  let seen = 0;
  for (let i = 0; i < pages.length; i += 1) {
    if (currentStep < seen + pages[i].length) {
      pageIndex = i;
      break;
    }
    seen += pages[i].length;
  }
  const firstStepOnPage = pages.slice(0, pageIndex).reduce((n, p) => n + p.length, 0);
  const page = pages[pageIndex];

  // Each page is scaled for its own contents, so a short page is not shrunk to
  // match the longest one.
  const scale = Math.max(MIN_SCALE, fitScale(page, available));
  const s = (value: number) => Math.round(value * scale);
  const padding = Math.max(40, Math.round(BASE_PADDING * Math.max(scale, 0.6)));

  return (
    <AbsoluteFill
      style={{ backgroundColor: BG, padding, justifyContent: 'center', fontFamily: font }}
    >
      {page.map((step, indexOnPage) => {
        const i = firstStepOnPage + indexOnPage;
        // Each step appears on its own beat and then stays — the point of a
        // process flow is seeing the whole chain at the end.
        const appearsAt = startFrame + i * perStep;
        const revealed = interpolate(frame - appearsAt, [0, 14], [0, 1], {
          extrapolateLeft: 'clamp',
          extrapolateRight: 'clamp',
        });
        return (
          <div
            key={i}
            style={{
              display: 'flex',
              alignItems: 'flex-start',
              gap: s(26),
              marginBottom: s(26),
              opacity: revealed,
              transform: `translateX(${interpolate(revealed, [0, 1], [-24, 0])}px)`,
            }}
          >
            <div
              style={{
                minWidth: s(52),
                height: s(52),
                borderRadius: s(26),
                backgroundColor: accent,
                color: FG,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                fontSize: s(26),
                fontWeight: 700,
                flexShrink: 0,
              }}
            >
              {i + 1}
            </div>
            <div>
              <div style={{ color: FG, fontSize: s(36), fontWeight: 600 }}>{step.label}</div>
              {step.detail ? (
                <div
                  style={{
                    color: MUTED,
                    fontSize: s(24),
                    marginTop: s(8),
                    maxWidth: 1000,
                  }}
                >
                  {step.detail}
                </div>
              ) : null}
            </div>
          </div>
        );
      })}
    </AbsoluteFill>
  );
};

const Chart: React.FC<{ chart: ChartSpec; accent: string; startFrame: number }> = ({
  chart,
  accent,
  startFrame,
}) => {
  const frame = useCurrentFrame();
  const grow = interpolate(frame - startFrame, [0, 40], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });

  const all = chart.series.flatMap((s) => s.values);
  // Guard against a flat series: dividing by a zero range would produce NaN
  // heights and an empty chart.
  const max = Math.max(...all, 0);
  const safeMax = max === 0 ? 1 : max;

  const palette = [accent, '#22d3ee', '#f59e0b', '#a78bfa'];

  return (
    <AbsoluteFill
      style={{ backgroundColor: BG, padding: 90, justifyContent: 'center', fontFamily: font }}
    >
      {chart.y_title ? (
        <div style={{ color: MUTED, fontSize: 22, marginBottom: 12 }}>{chart.y_title}</div>
      ) : null}

      <div
        style={{
          display: 'flex',
          alignItems: 'flex-end',
          gap: 18,
          height: 460,
          borderBottom: `2px solid ${MUTED}`,
          paddingBottom: 4,
        }}
      >
        {chart.x_labels.map((label, i) => (
          <div key={i} style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 6 }}>
            <div style={{ display: 'flex', alignItems: 'flex-end', gap: 4, height: 460 }}>
              {chart.series.map((s, si) => (
                <div
                  key={si}
                  style={{
                    flex: 1,
                    height: `${((s.values[i] ?? 0) / safeMax) * 100 * grow}%`,
                    backgroundColor: palette[si % palette.length],
                    borderRadius: '4px 4px 0 0',
                  }}
                />
              ))}
            </div>
          </div>
        ))}
      </div>

      <div style={{ display: 'flex', gap: 18, marginTop: 10 }}>
        {chart.x_labels.map((label, i) => (
          <div key={i} style={{ flex: 1, color: MUTED, fontSize: 20, textAlign: 'center' }}>
            {label}
          </div>
        ))}
      </div>

      {chart.series.length > 1 ? (
        <div style={{ display: 'flex', gap: 24, marginTop: 28 }}>
          {chart.series.map((s, si) => (
            <div key={si} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <div
                style={{
                  width: 16,
                  height: 16,
                  backgroundColor: palette[si % palette.length],
                  borderRadius: 3,
                }}
              />
              <span style={{ color: MUTED, fontSize: 22 }}>{s.name}</span>
            </div>
          ))}
        </div>
      ) : null}
    </AbsoluteFill>
  );
};

export const Lesson: React.FC<{ spec: LessonSpec }> = ({ spec }) => {
  const titleFrames = Math.round(2.5 * FPS);
  let cursor = titleFrames;

  const sections: React.ReactNode[] = [
    <Sequence key="title" from={0} durationInFrames={titleFrames}>
      <TitleCard spec={spec} />
    </Sequence>,
  ];

  for (const [i, slide] of spec.slides.entries()) {
    const length = Math.round(slide.seconds * FPS);
    sections.push(
      <Sequence key={`slide-${i}`} from={cursor} durationInFrames={length}>
        <SlideCard slide={slide} accent={spec.accent} start={0} length={length} />
      </Sequence>,
    );
    cursor += length;
  }

  if (spec.steps.length > 0) {
    const length = spec.steps.length * 3 * FPS;
    sections.push(
      <Sequence key="steps" from={cursor} durationInFrames={length}>
        <StepsScene steps={spec.steps} accent={spec.accent} startFrame={0} />
      </Sequence>,
    );
    cursor += length;
  }

  if (spec.chart) {
    const length = Math.round(6 * FPS);
    sections.push(
      <Sequence key="chart" from={cursor} durationInFrames={length}>
        <Chart chart={spec.chart} accent={spec.accent} startFrame={0} />
      </Sequence>,
    );
    cursor += length;
  }

  return <AbsoluteFill style={{ backgroundColor: BG }}>{sections}</AbsoluteFill>;
};

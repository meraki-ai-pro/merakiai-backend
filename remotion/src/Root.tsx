import React from 'react';
import { Composition } from 'remotion';
import { Lesson } from './Lesson';
import { CHART_SECONDS, DEFAULT_SPEC, FPS, STEP_SECONDS, type LessonSpec } from './types';

/**
 * Duration is computed from the spec, mirroring
 * RemotionSpec.duration_seconds on the Python side.
 *
 * The CLI also passes --frames, so a disagreement between the two would cut a
 * video short rather than fail — which is why both derive it the same way.
 */
function durationInFrames(spec: LessonSpec): number {
  const fromSlides = spec.slides.reduce((total, s) => total + s.seconds, 0);
  const fromSteps = spec.steps.length * STEP_SECONDS;
  const fromChart = spec.chart ? CHART_SECONDS : 0;
  const seconds = 3 + fromSlides + fromSteps + fromChart + 2.5;
  return Math.max(Math.round(seconds * FPS), FPS);
}

export const RemotionRoot: React.FC = () => (
  <Composition
    id="Lesson"
    component={Lesson}
    durationInFrames={durationInFrames(DEFAULT_SPEC)}
    fps={FPS}
    width={1280}
    height={720}
    defaultProps={{ spec: DEFAULT_SPEC }}
    calculateMetadata={({ props }) => ({
      durationInFrames: durationInFrames(props.spec),
      fps: FPS,
    })}
  />
);

import React from 'react';
import { Composition } from 'remotion';
import { Lesson } from './Lesson';
import { DEFAULT_SPEC, FPS, type LessonSpec } from './types';

/**
 * Duration is computed from the spec, mirroring
 * RemotionSpec.duration_seconds on the Python side.
 *
 * The CLI also passes --frames, so a disagreement between the two would cut a
 * video short rather than fail — which is why both derive it the same way.
 */
function durationInFrames(spec: LessonSpec): number {
  const fromSlides = spec.slides.reduce((total, s) => total + s.seconds, 0);
  const fromSteps = spec.steps.length * 3;
  const fromChart = spec.chart ? 6 : 0;
  const seconds = 2.5 + fromSlides + fromSteps + fromChart + 1.5;
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

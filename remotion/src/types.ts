/**
 * The spec shape. Mirrors app/media/render/remotion_spec.py — the Python side
 * validates, this side renders, and the two must agree.
 *
 * Nothing here is generated code. A spec is data, so a malformed one produces
 * a failed validation rather than an execution.
 */

export type Archetype =
  | 'data_story'
  | 'process_flow'
  | 'composited_explainer'
  | 'timeline'
  | 'ui_or_code_walkthrough';

export interface Slide {
  title: string;
  body?: string | null;
  seconds: number;
}

export interface Step {
  label: string;
  detail?: string | null;
}

export interface Series {
  name: string;
  values: number[];
}

export interface ChartSpec {
  kind: 'bar' | 'line' | 'area';
  x_labels: string[];
  series: Series[];
  x_title?: string | null;
  y_title?: string | null;
}

export interface LessonSpec {
  archetype: Archetype;
  title: string;
  subtitle?: string | null;
  slides: Slide[];
  steps: Step[];
  chart?: ChartSpec | null;
  accent: string;
}

export const FPS = 30;

/**
 * Seconds each process/timeline step holds the screen, and how long a chart
 * gets. Mirrors STEP_SECONDS / CHART_SECONDS in
 * app/media/render/remotion_spec.py.
 *
 * These MUST match the Python values: the CLI is given a frame range derived
 * from the Python duration, and the composition computes its own
 * durationInFrames from these. A disagreement is not a slightly-wrong video,
 * it is a hard render failure ("frame range is not inbetween").
 */
export const STEP_SECONDS = 4.5;
export const CHART_SECONDS = 8;

/** Fallback used when Remotion Studio is opened without --props. */
export const DEFAULT_SPEC: LessonSpec = {
  archetype: 'process_flow',
  title: 'Lesson',
  subtitle: 'Open with --props to render a real spec',
  slides: [],
  steps: [{ label: 'No spec supplied' }],
  chart: null,
  accent: '#2563eb',
};

import React from 'react';
import { AbsoluteFill, Img, Sequence, interpolate, spring, staticFile, useCurrentFrame, useVideoConfig } from 'remotion';

/**
 * The 30-second product intro on the landing page: the two modes, the formats
 * a lesson comes in, and what instructors see. Rendered once to an MP4 that the
 * frontend serves from /public; not part of the lesson pipeline.
 *
 *   npx remotion render src/index.ts Intro out/meraki-intro.mp4
 */

export const INTRO_FPS = 30;
export const INTRO_SECONDS = 30;

const BLUE = '#2563eb';
const CYAN = '#67e8f9';
const INK = '#0f172a';
const MUTED = '#64748b';
const PAPER = '#f8fafc';
const font = 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif';

/** Scene boundaries in seconds; they add up to INTRO_SECONDS. */
const SCENES = [
  { from: 0, to: 4.5 },   // welcome
  { from: 4.5, to: 12 },  // modes
  { from: 12, to: 19 },   // formats
  { from: 19, to: 26 },   // analytics
  { from: 26, to: 30 },   // sign-off
] as const;

function useEnter(delay = 0) {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  return spring({ frame: frame - delay, fps, config: { damping: 18, mass: 0.7 } });
}

/** Fade each scene in and out so the cuts read as one continuous film. */
function SceneFade({ seconds, children }: { seconds: number; children: React.ReactNode }) {
  const frame = useCurrentFrame();
  const total = seconds * INTRO_FPS;
  const opacity = interpolate(frame, [0, 10, total - 10, total], [0, 1, 1, 0], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  return <AbsoluteFill style={{ opacity }}>{children}</AbsoluteFill>;
}

function Heading({ kicker, title }: { kicker: string; title: string }) {
  const enter = useEnter();
  return (
    <div style={{ transform: `translateY(${(1 - enter) * 30}px)`, opacity: enter }}>
      <div style={{ color: BLUE, fontSize: 26, fontWeight: 700, letterSpacing: 3, textTransform: 'uppercase' }}>{kicker}</div>
      <div style={{ color: INK, fontSize: 56, fontWeight: 800, marginTop: 6 }}>{title}</div>
    </div>
  );
}

function Card({ delay, title, lines, accent = BLUE }: { delay: number; title: string; lines: string[]; accent?: string }) {
  const enter = useEnter(delay);
  return (
    <div
      style={{
        flex: 1, background: 'white', borderRadius: 28, padding: 36,
        boxShadow: '0 24px 60px rgba(15,23,42,0.12)', borderTop: `8px solid ${accent}`,
        transform: `translateY(${(1 - enter) * 60}px) scale(${0.94 + enter * 0.06})`, opacity: enter,
      }}
    >
      <div style={{ fontSize: 40, fontWeight: 800, color: INK }}>{title}</div>
      {lines.map((line, i) => (
        <div key={line} style={{ fontSize: 26, color: MUTED, marginTop: i ? 10 : 18, opacity: interpolate(enter, [0.5, 1], [0, 1], { extrapolateLeft: 'clamp' }) }}>
          • {line}
        </div>
      ))}
    </div>
  );
}

function Welcome() {
  const enter = useEnter();
  const frame = useCurrentFrame();
  const glow = 0.5 + 0.5 * Math.sin(frame / 12);
  return (
    <AbsoluteFill style={{ alignItems: 'center', justifyContent: 'center', background: `linear-gradient(135deg, ${INK}, #1e3a8a)` }}>
      <div
        style={{
          width: 150, height: 150, borderRadius: 44, background: BLUE, display: 'flex', alignItems: 'center',
          justifyContent: 'center',
          boxShadow: `0 0 ${40 + glow * 40}px rgba(103,232,249,${0.35 + glow * 0.25})`,
          transform: `scale(${enter})`,
        }}
      >
        <Img src={staticFile('meraki-icon-white.png')} style={{ width: 96, height: 96 }} />
      </div>
      <div style={{ color: 'white', fontSize: 84, fontWeight: 800, marginTop: 34, opacity: enter }}>Meraki</div>
      <div style={{ color: CYAN, fontSize: 34, marginTop: 10, opacity: interpolate(frame, [20, 40], [0, 1], { extrapolateRight: 'clamp' }) }}>
        Your AI study tutor, built on your course
      </div>
    </AbsoluteFill>
  );
}

function Modes() {
  return (
    <AbsoluteFill style={{ background: PAPER, padding: '56px 72px' }}>
      <Heading kicker="Two ways to study" title="Learn, then Review" />
      <div style={{ display: 'flex', gap: 40, marginTop: 50 }}>
        <Card delay={14} title="Learn" lines={['Ask anything about the course', 'Step-by-step worked examples', 'Answers cite the lecture notes']} />
        <Card delay={30} accent="#0891b2" title="Review" lines={['Multiple choice & fill-the-blank', 'Short answers, marked instantly', 'Guided scenarios with feedback']} />
      </div>
    </AbsoluteFill>
  );
}

function Formats() {
  const frame = useCurrentFrame();
  const items = [
    { icon: '📝', name: 'Text', note: 'Read at your pace' },
    { icon: '🎬', name: 'Video', note: 'Animated explanations' },
    { icon: '🔊', name: 'Listen', note: 'Narrated aloud' },
    { icon: '📈', name: 'Lesson board', note: 'Slides & live graphs' },
  ];
  // A curve drawing itself on the board, for the "live graph".
  const progress = interpolate(frame, [40, 150], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
  const points = Array.from({ length: 60 }, (_, i) => {
    const x = (i / 59) * progress;
    return `${40 + x * 420},${200 - Math.sin(x * Math.PI * 1.5) * 120 * x}`;
  }).join(' ');
  return (
    <AbsoluteFill style={{ background: PAPER, padding: '56px 72px' }}>
      <Heading kicker="Every lesson, your way" title="Text, video, audio or board" />
      <div style={{ display: 'flex', gap: 32, marginTop: 36, height: 430 }}>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gridTemplateRows: '1fr 1fr', gap: 20, flex: 1.2 }}>
          {items.map((it, i) => {
            const enter = spring({ frame: frame - 12 - i * 10, fps: INTRO_FPS, config: { damping: 16 } });
            return (
              <div key={it.name} style={{ background: 'white', borderRadius: 22, padding: '20px 24px', boxShadow: '0 16px 40px rgba(15,23,42,0.10)', opacity: enter, transform: `scale(${0.9 + enter * 0.1})` }}>
                <div style={{ fontSize: 38 }}>{it.icon}</div>
                <div style={{ fontSize: 28, fontWeight: 800, color: INK, marginTop: 4 }}>{it.name}</div>
                <div style={{ fontSize: 22, color: MUTED }}>{it.note}</div>
              </div>
            );
          })}
        </div>
        <div style={{ flex: 1, background: INK, borderRadius: 26, padding: 24 }}>
          <div style={{ color: CYAN, fontSize: 22, fontWeight: 700 }}>Lesson board</div>
          <svg width="100%" height="360" viewBox="0 0 500 300">
            <line x1="40" y1="200" x2="470" y2="200" stroke="#334155" strokeWidth="3" />
            <line x1="40" y1="20" x2="40" y2="280" stroke="#334155" strokeWidth="3" />
            <polyline points={points} fill="none" stroke={CYAN} strokeWidth="6" strokeLinecap="round" />
          </svg>
        </div>
      </div>
    </AbsoluteFill>
  );
}

function Analytics() {
  const frame = useCurrentFrame();
  const bars = [
    { topic: 'Limits', value: 0.82 },
    { topic: 'Derivatives', value: 0.64 },
    { topic: 'Integrals', value: 0.41 },
    { topic: 'Series', value: 0.27 },
  ];
  const stats = [
    { label: 'Students active', value: 42 },
    { label: 'Questions answered', value: 1280 },
    { label: 'Minutes studied', value: 3650 },
  ];
  return (
    <AbsoluteFill style={{ background: PAPER, padding: '56px 72px' }}>
      <Heading kicker="For instructors" title="See who needs help, and why" />
      <div style={{ display: 'flex', gap: 32, marginTop: 36, alignItems: 'flex-start' }}>
        <div style={{ flex: 1.3 }}>
        <div style={{ background: 'white', borderRadius: 26, padding: 32, boxShadow: '0 16px 40px rgba(15,23,42,0.10)' }}>
          <div style={{ fontSize: 26, fontWeight: 700, color: INK, marginBottom: 18 }}>Topic mastery</div>
          {bars.map((b, i) => {
            const grow = interpolate(frame, [20 + i * 8, 70 + i * 8], [0, b.value], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
            const colour = b.value >= 0.7 ? '#10b981' : b.value >= 0.4 ? '#f59e0b' : '#ef4444';
            return (
              <div key={b.topic} style={{ display: 'flex', alignItems: 'center', gap: 16, marginTop: 16 }}>
                <div style={{ width: 150, fontSize: 22, color: MUTED }}>{b.topic}</div>
                <div style={{ flex: 1, height: 28, background: '#e2e8f0', borderRadius: 14 }}>
                  <div style={{ width: `${grow * 100}%`, height: '100%', background: colour, borderRadius: 14 }} />
                </div>
                <div style={{ width: 70, fontSize: 22, fontWeight: 700, color: INK }}>{Math.round(grow * 100)}%</div>
              </div>
            );
          })}
        </div>
          <div style={{ fontSize: 24, color: MUTED, marginTop: 22, opacity: interpolate(frame, [120, 150], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' }) }}>
            Exams, interventions and a Data Center for every report.
          </div>
        </div>
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 18 }}>
          {stats.map((s, i) => {
            const n = Math.round(interpolate(frame, [30 + i * 10, 100 + i * 10], [0, s.value], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' }));
            return (
              <div key={s.label} style={{ background: 'white', borderRadius: 22, padding: '16px 24px', boxShadow: '0 16px 40px rgba(15,23,42,0.10)' }}>
                <div style={{ fontSize: 42, fontWeight: 800, color: BLUE }}>{n.toLocaleString('en-GB')}</div>
                <div style={{ fontSize: 22, color: MUTED }}>{s.label}</div>
              </div>
            );
          })}
        </div>
      </div>
    </AbsoluteFill>
  );
}

function SignOff() {
  const enter = useEnter();
  return (
    <AbsoluteFill style={{ alignItems: 'center', justifyContent: 'center', background: `linear-gradient(135deg, ${BLUE}, #0e7490)` }}>
      <div style={{ color: 'white', fontSize: 76, fontWeight: 800, transform: `scale(${0.9 + enter * 0.1})`, opacity: enter }}>
        Learn smarter with Meraki
      </div>
      <div style={{ color: '#e0f2fe', fontSize: 32, marginTop: 20, opacity: enter }}>
        Join with the invite code from your instructor
      </div>
    </AbsoluteFill>
  );
}

const SCENE_COMPONENTS = [Welcome, Modes, Formats, Analytics, SignOff];

export const Intro: React.FC = () => (
  <AbsoluteFill style={{ fontFamily: font, background: PAPER }}>
    {SCENES.map(({ from, to }, i) => {
      const Scene = SCENE_COMPONENTS[i];
      return (
        <Sequence key={i} from={Math.round(from * INTRO_FPS)} durationInFrames={Math.round((to - from) * INTRO_FPS)}>
          <SceneFade seconds={to - from}>
            <Scene />
          </SceneFade>
        </Sequence>
      );
    })}
  </AbsoluteFill>
);

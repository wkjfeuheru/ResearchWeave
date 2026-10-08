# Existing UI components
The existing dashboard has no shared form/dialog primitives. The new web app will use dedicated primitives.

## autopilot-dashboard/src/components/PipelineAnimation.tsx
```
/**
 * PipelineAnimation — a rich, orbital visualization of the autopilot pipeline.
 *
 * Central hub surrounded by 5 stage nodes in a circuit layout, with
 * data packets flowing through connecting paths, background grid,
 * ambient particles, and pulsing energy rings. Fills the entire
 * container with minimal dead space.
 */
export function PipelineAnimation() {
  const cx = 200;
  const cy = 105;
  const rx = 145;
  const ry = 65;

  const stages = [
    { label: "QUEUE", angle: Math.PI },
    { label: "PREP", angle: Math.PI + Math.PI * 2 / 5 },
    { label: "RUN", angle: Math.PI + (Math.PI * 2 / 5) * 2 },
    { label: "CHECK", angle: Math.PI + (Math.PI * 2 / 5) * 3 },
    { label: "MERGE", angle: Math.PI + (Math.PI * 2 / 5) * 4 },
  ].map((s) => ({
    ...s,
    x: cx + Math.cos(s.angle) * rx,
    y: cy + Math.sin(s.angle) * ry,
  }));

  // Build the orbital path for traveling packets
  const orbitPath = stages
    .map((s, i) => `${i === 0 ? "M" : "L"} ${s.x.toFixed(1)} ${s.y.toFixed(1)}`)
    .join(" ") + " Z";

  // Ambient floating particles
  const particles = [
    { x: 40, y: 30, r: 1.2, dur: 3, d: 0 },
    { x: 340, y: 50, r: 0.8, dur: 4, d: 1 },
    { x: 80, y: 170, r: 1, dur: 3.5, d: 2 },
    { x: 320, y: 160, r: 1.3, dur: 2.8, d: 0.5 },
    { x: 170, y: 25, r: 0.7, dur: 4.2, d: 1.5 },
    { x: 250, y: 185, r: 0.9, dur: 3.2, d: 3 },
    { x: 120, y: 100, r: 0.6, dur: 5, d: 2.5 },
    { x: 300, y: 110, r: 1.1, dur: 3.8, d: 0.8 },
    { x: 50, y: 120, r: 0.8, dur: 4.5, d: 1.2 },
    { x: 370, y: 90, r: 0.7, dur: 3.6, d: 2.2 },
  ];

  // Background grid lines
  const hLines = [35, 70, 105, 140, 175];
  const vLines = [40, 80, 120, 160, 200, 240, 280, 320, 360];

  return (
    <svg
      viewBox="0 0 400 210"
      xmlns="http://www.w3.org/2000/svg"
      aria-label="Autopilot pipeline"
      style={{ width: "100%", height: "100%" }}
      preserveAspectRatio="xMidYMid meet"
    >
      <defs>
        <radialGradient id="pipe-glow" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="#00d4aa" stopOpacity="0.85" />
          <stop offset="40%" stopColor="#00d4aa" stopOpacity="0.3" />
          <stop offset="100%" stopColor="#00d4aa" stopOpacity="0" />
        </radialGradient>
        <radialGradient id="hub-glow" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="#00d4aa" stopOpacity="0.2" />
          <stop offset="100%" stopColor="#00d4aa" stopOpacity="0" />
        </radialGradient>
        <radialGradient id="node-glow" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="#00d4aa" stopOpacity="0.15" />
          <stop offset="100%" stopColor="#00d4aa" stopOpacity="0" />
        </radialGradient>
        <linearGradient id="edge-fade" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0%" stopColor="#00d4aa" stopOpacity="0" />
          <stop offset="50%" stopColor="#00d4aa" stopOpacity="0.25" />
          <stop offset="100%" stopColor="#00d4aa" stopOpacity="0" />
        </linearGradient>
      </defs>

      {/* Background grid */}
      {hLines.map((y, i) => (
        <line key={`h${i}`} x1="0" y1={y} x2="400" y2={y}
          stroke="#00d4aa" strokeOpacity="0.04" strokeWidth="1" />
      ))}
      {vLines.map((x, i) => (
        <line key={`v${i}`} x1={x} y1="0" x2={x} y2="210"
          stroke="#00d4aa" strokeOpacity="0.04" strokeWidth="1" />
      ))}

      {/* Central radial glow */}
      <circle cx={cx} cy={cy} r="80" fill="url(#hub-glow)" />

      {/* Orbital ring (background) */}
      <path d={orbitPath} fill="none" stroke="#00d4aa" strokeOpacity="0.08" strokeWidth="1" />

      {/* Animated dashed orbital ring */}
      <path d={orbitPath} fill="none" stroke="#00d4aa" strokeOpacity="0.18"
        strokeWidth="1" strokeDasharray="4 8">
        <animate attributeName="stroke-dashoffset" values="0;-24" dur="2s" repeatCount="indefinite" />
      </path>

      {/* Spokes from center to each node */}
      {stages.map((s, i) => (
        <g key={`spoke-${i}`}>
          <line x1={cx} y1={cy} x2={s.x} y2={s.y}
            stroke="#00d4aa" strokeOpacity="0.06" strokeWidth="1" />
          {/* Data pulse along spoke */}
          <circle r="1.5" fill="#00d4aa">
            <animateMotion
              dur="2s"
              begin={`${i * 0.4}s`}
              repeatCount="indefinite"
              path={`M ${cx} ${cy} L ${s.x} ${s.y}`}
            />
            <animate attributeName="opacity" values="0;0.8;0.8;0"
              keyTimes="0;0.1;0.8;1" dur="2s" begin={`${i * 0.4}s`} repeatCount="indefinite" />
          </circle>
        </g>
      ))}

      {/* Stage nodes */}
      {stages.map((s, i) => {
        const colors = ["#64748b", "#0f766e", "#00d4aa", "#3b82f6", "#8b5cf6"];
        const c = colors[i];
        return (
          <g key={s.label}>
            {/* Node glow */}
            <circle cx={s.x} cy={s.y} r="28" fill="url(#node-glow)" />

            {/* Outer ring */}
            <circle cx={s.x} cy={s.y} r="18" fill="#0a0a0a"
              stroke={c} strokeOpacity="0.5" strokeWidth="1" />

            {/* Corner brackets */}
            <g stroke={c} strokeOpacity="0.7" strokeWidth="1" strokeLinecap="round">
              <path d={`M ${s.x-13} ${s.y-13} l 5 0 M ${s.x-13} ${s.y-13} l 0 5`} />
              <path d={`M ${s.x+13} ${s.y-13} l -5 0 M ${s.x+13} ${s.y-13} l 0 5`} />
              <path d={`M ${s.x-13} ${s.y+13} l 5 0 M ${s.x-13} ${s.y+13} l 0 -5`} />
              <path d={`M ${s.x+13} ${s.y+13} l -5 0 M ${s.x+13} ${s.y+13} l 0 -5`} />
            </g>

            {/* Inner pulsing dot */}
            <circle cx={s.x} cy={s.y} r="4" fill={c}>
              <animate attributeName="r" values="4;6;4" dur="2.4s"
                begin={`${i * 0.5}s`} repeatCount="indefinite" />
              <animate attributeName="opacity" values="0.5;1;0.5" dur="2.4s"
                begin={`${i * 0.5}s`} repeatCount="indefinite" />
            </circle>

            {/* Expanding pulse ring */}
            <circle cx={s.x} cy={s.y} r="18" fill="none" stroke={c} strokeWidth="1">
              <animate attributeName="r" values="18;30" dur="3s"
                begin={`${i * 0.6}s`} repeatCount="indefinite" />
              <animate attributeName="opacity" values="0.45;0" dur="3s"
                begin={`${i * 0.6}s`} repeatCount="indefinite" />
            </circle>

            {/* Label */}
            <text x={s.x} y={s.y + 30} fontSize="7" fill={c} fillOpacity="0.7"
              textAnchor="middle" fontFamily="JetBrains Mono, monospace"
              letterSpacing="1.5" fontWeight="700">
              {s.label}
            </text>
          </g>
        );
      })}

      {/* Central hub */}
      <circle cx={cx} cy={cy} r="14" fill="#0a0a0a" stroke="#00d4aa" strokeOpacity="0.6" strokeWidth="1.2" />
      <circle cx={cx} cy={cy} r="14" fill="none" stroke="#00d4aa" strokeWidth="1">
        <animate attributeName="r" values="14;22" dur="2.4s" repeatCount="indefinite" />
        <animate attributeName="opacity" values="0.4;0" dur="2.4s" repeatCount="indefinite" />
      </circle>
      {/* Hub icon — infinity / loop symbol */}
      <text x={cx} y={cy + 4} fontSize="11" fill="#00d4aa" fillOpacity="0.9"
        textAnchor="middle" fontFamily="JetBrains Mono, monospace" fontWeight="700">
        &#x221E;
      </text>

      {/* Traveling packet 1 — clockwise */}
      <circle r="16" fill="url(#pipe-glow)" opacity="0.6">
        <animateMotion dur="5s" repeatCount="indefinite" path={orbitPath} rotate="auto" />
      </circle>
      <circle r="3.5" fill="#00d4aa">
        <animateMotion dur="5s" repeatCount="indefinite" path={orbitPath} rotate="auto" />
        <animate attributeName="r" values="3.5;2.5;3.5" dur="1s" repeatCount="indefinite" />
      </circle>

      {/* Traveling packet 2 — offset, different speed */}
      <circle r="10" fill="url(#pipe-glow)" opacity="0.35">
        <animateMotion dur="7s" begin="2.5s" repeatCount="indefinite" path={orbitPath} rotate="auto" />
      </circle>
      <circle r="2" fill="#8b5cf6">
        <animateMotion dur="7s" begin="2.5s" repeatCount="indefinite" path={orbitPath} rotate="auto" />
        <animate attributeName="opacity" values="0.4;1;0.4" dur="2s" repeatCount="indefinite" />
      </circle>

      {/* Ambient particles */}
      {particles.map((p, i) => (
        <circle key={`p${i}`} cx={p.x} cy={p.y} r={p.r}
          fill={i % 3 === 0 ? "#8b5cf6" : "#00d4aa"}>
          <animate attributeName="opacity" values="0.05;0.25;0.05"
            dur={`${p.dur}s`} begin={`${p.d}s`} repeatCount="indefinite" />
          <animate attributeName="r" values={`${p.r};${p.r * 1.5};${p.r}`}
            dur={`${p.dur}s`} begin={`${p.d}s`} repeatCount="indefinite" />
        </circle>
      ))}

      {/* Scan line */}
      <line x1="0" y1="0" x2="400" y2="0" stroke="#00d4aa" strokeOpacity="0" strokeWidth="1">
        <animateTransform attributeName="transform" type="translate"
          from="0 0" to="0 210" dur="4s" repeatCount="indefinite" />
        <animate attributeName="stroke-opacity" values="0;0.12;0.12;0"
          keyTimes="0;0.05;0.95;1" dur="4s" repeatCount="indefinite" />
      </line>

      {/* Bottom caption */}
      <text x={cx} y="204" fontSize="7" fill="#00d4aa" fillOpacity="0.4"
        textAnchor="middle" fontFamily="JetBrains Mono, monospace" letterSpacing="2.5">
        AUTOPILOT · PIPELINE
      </text>
    </svg>
  );
}

```

## autopilot-dashboard/src/components/HeroBackground.tsx
```
/**
 * CyberHeroBackground — full-bleed animated SVG background for the kanban hero.
 *
 * Layers (back → front):
 *  1. Radial teal glow
 *  2. Perspective grid floor
 *  3. Horizontal data-stream lines
 *  4. Constellation nodes + edges
 *  5. Data fragment segments
 *  6. Binary / hex rain
 *  7. Horizontal scan-line
 *
 * All SMIL-based — no JS animation loops needed.
 */
export function HeroBackground() {
  const glyphs = [
    { x: 45, ch: "0", sz: 11, dur: 14, d: 0 },
    { x: 120, ch: "1", sz: 9, dur: 18, d: 3 },
    { x: 195, ch: "A", sz: 10, dur: 12, d: 7 },
    { x: 290, ch: "F", sz: 8, dur: 16, d: 1 },
    { x: 365, ch: "0", sz: 12, dur: 20, d: 5 },
    { x: 430, ch: "1", sz: 9, dur: 13, d: 9 },
    { x: 510, ch: "D", sz: 10, dur: 17, d: 2 },
    { x: 580, ch: "0", sz: 11, dur: 15, d: 6 },
    { x: 650, ch: "1", sz: 8, dur: 11, d: 4 },
    { x: 720, ch: "B", sz: 10, dur: 19, d: 8 },
    { x: 790, ch: "0", sz: 9, dur: 14, d: 1 },
    { x: 860, ch: "E", sz: 11, dur: 16, d: 10 },
    { x: 930, ch: "1", sz: 10, dur: 12, d: 3 },
    { x: 1000, ch: "C", sz: 8, dur: 18, d: 7 },
    { x: 1070, ch: "0", sz: 12, dur: 15, d: 0 },
    { x: 1140, ch: "7", sz: 9, dur: 13, d: 5 },
    { x: 260, ch: "3", sz: 8, dur: 22, d: 11 },
    { x: 475, ch: "F", sz: 10, dur: 16, d: 4 },
    { x: 690, ch: "8", sz: 9, dur: 20, d: 6 },
    { x: 1020, ch: "1", sz: 11, dur: 14, d: 8 },
  ];

  const cn = [
    { x: 80, y: 55 },
    { x: 210, y: 30 },
    { x: 355, y: 80 },
    { x: 500, y: 45 },
    { x: 700, y: 68 },
    { x: 850, y: 28 },
    { x: 980, y: 60 },
    { x: 1120, y: 42 },
    { x: 145, y: 140 },
    { x: 420, y: 155 },
    { x: 780, y: 145 },
    { x: 1050, y: 130 },
  ];

  const ce: [number, number][] = [
    [0, 1], [1, 2], [2, 3], [3, 4], [4, 5], [5, 6], [6, 7],
    [8, 9], [9, 10], [10, 11],
    [0, 8], [3, 9], [4, 10], [7, 11],
  ];

  const streams = [
    { y: 90, dash: "4 18", tot: 22, spd: 2.0, op: 0.07 },
    { y: 160, dash: "2 22", tot: 24, spd: 2.8, op: 0.05 },
    { y: 230, dash: "6 14", tot: 20, spd: 1.5, op: 0.08 },
    { y: 300, dash: "3 20", tot: 23, spd: 2.2, op: 0.06 },
    { y: 370, dash: "5 15", tot: 20, spd: 1.8, op: 0.07 },
  ];

  const frags = [
    { x: 95, y: 120, w: 40 },
    { x: 310, y: 200, w: 30 },
    { x: 540, y: 280, w: 50 },
    { x: 760, y: 130, w: 35 },
    { x: 990, y: 250, w: 45 },
    { x: 180, y: 350, w: 30 },
    { x: 640, y: 380, w: 40 },
    { x: 1080, y: 330, w: 35 },
  ];

  const gridY = [300, 325, 355, 390, 430];
  const vx = 600;
  const vy = 240;
  const radials = [-5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5];

  return (
    <svg
      viewBox="0 0 1200 460"
      preserveAspectRatio="xMidYMid slice"
      style={{ width: "100%", height: "100%" }}
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
    >
      <defs>
        <radialGradient id="bg-glow" cx="50%" cy="35%" r="45%">
          <stop offset="0%" stopColor="#00d4aa" stopOpacity="0.12" />
          <stop offset="50%" stopColor="#00d4aa" stopOpacity="0.03" />
          <stop offset="100%" stopColor="#00d4aa" stopOpacity="0" />
        </radialGradient>
        <linearGradient id="bg-vfade" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="white" stopOpacity="0" />
          <stop offset="8%" stopColor="white" stopOpacity="1" />
          <stop offset="88%" stopColor="white" stopOpacity="1" />
          <stop offset="100%" stopColor="white" stopOpacity="0" />
        </linearGradient>
        <mask id="bg-mask">
          <rect width="1200" height="460" fill="url(#bg-vfade)" />
        </mask>
      </defs>

      {/* Central glow */}
      <rect width="1200" height="460" fill="url(#bg-glow)" />

      <g mask="url(#bg-mask)">
        {/* 1. Perspective grid */}
        {gridY.map((y, i) => (
          <line key={`hg${i}`} x1="50" y1={y} x2="1150" y2={y}
            stroke="#00d4aa" strokeOpacity={0.025 + i * 0.012} strokeWidth="1" />
        ))}
        {radials.map((n) => (
          <line key={`vg${n}`} x1={vx} y1={vy} x2={vx + n * 130} y2="460"
            stroke="#00d4aa" strokeOpacity="0.025" strokeWidth="1" />
        ))}

        {/* 2. Data streams */}
        {streams.map((s, i) => (
          <line key={`ds${i}`} x1="0" y1={s.y} x2="1200" y2={s.y}
            stroke="#00d4aa" strokeOpacity={s.op} strokeWidth="1" strokeDasharray={s.dash}>
            <animate attributeName="stroke-dashoffset" from="0" to={`-${s.tot}`}
              dur={`${s.spd}s`} repeatCount="indefinite" />
          </line>
        ))}

        {/* 3. Constellation edges */}
        {ce.map(([a, b], i) => (
          <line key={`ce${i}`} x1={cn[a].x} y1={cn[a].y} x2={cn[b].x} y2={cn[b].y}
            stroke="#00d4aa" strokeOpacity="0.06" strokeWidth="1" />
        ))}

        {/* 4. Constellation nodes */}
        {cn.map((n, i) => (
          <circle key={`cn${i}`} cx={n.x} cy={n.y} r="2" fill="#00d4aa">
            <animate attributeName="fill-opacity" values="0.1;0.35;0.1"
              dur={`${2 + (i % 4) * 0.5}s`} begin={`${i * 0.3}s`} repeatCount="indefinite" />
            <animate attributeName="r" values="1.5;3.5;1.5"
              dur={`${2 + (i % 4) * 0.5}s`} begin={`${i * 0.3}s`} repeatCount="indefinite" />
          </circle>
        ))}

        {/* 5. Data fragments */}
        {frags.map((f, i) => (
          <line key={`fr${i}`} x1={f.x} y1={f.y} x2={f.x + f.w} y2={f.y}
            stroke={i % 3 === 0 ? "#8b5cf6" : "#00d4aa"} strokeOpacity="0.08"
            strokeWidth="1" strokeLinecap="round">
            <animate attributeName="stroke-opacity" values="0.04;0.16;0.04"
              dur={`${2.5 + (i % 3) * 0.7}s`} begin={`${i * 0.4}s`} repeatCount="indefinite" />
          </line>
        ))}

        {/* 6. Binary / hex rain */}
        {glyphs.map((g, i) => (
          <text key={`gl${i}`} x={g.x} y={-10} fontSize={g.sz}
            fill={i % 7 === 0 ? "#8b5cf6" : i % 11 === 0 ? "#ff6b35" : "#00d4aa"}
            fillOpacity="0" fontFamily="JetBrains Mono, monospace" fontWeight="600" letterSpacing="0.6">
            {g.ch}
            <animateTransform attributeName="transform" type="translate"
              from="0 0" to="0 480" dur={`${g.dur}s`} begin={`${g.d}s`} repeatCount="indefinite" />
            <animate attributeName="fill-opacity" values="0;0.2;0.2;0"
              keyTimes="0;0.08;0.85;1" dur={`${g.dur}s`} begin={`${g.d}s`} repeatCount="indefinite" />
          </text>
        ))}

        {/* 7. Scan-line */}
        <line x1="0" y1="0" x2="1200" y2="0" stroke="#00d4aa" strokeOpacity="0" strokeWidth="1">
          <animateTransform attributeName="transform" type="translate"
            from="0 0" to="0 460" dur="6s" repeatCount="indefinite" />
          <animate attributeName="stroke-opacity" values="0;0.16;0.16;0"
            keyTimes="0;0.04;0.96;1" dur="6s" repeatCount="indefinite" />
        </line>
      </g>
    </svg>
  );
}

```

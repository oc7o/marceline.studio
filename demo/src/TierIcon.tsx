/**
 * The subscription tiers as one drawing at three counts, copied path for path from the design
 * system: Pro is the brand mark's pair of upper fangs, Max adds the lower pair, Ultra adds a small
 * tooth either side of both. `size` is the height of the upper fangs, not of the whole glyph, so
 * one number gives all three the same optical weight.
 */
const MARK = ["M0,0 L50,80 L100,0 L84,0 L50,47 L16,0 Z", "M114,0 L164,80 L214,0 L198,0 L164,47 L130,0 Z"];

const ART = {
  pro: { w: 214, h: 80, d: MARK },
  max: {
    w: 214,
    h: 157,
    d: [...MARK, "M24,157 L64,98 L104,157 L90,157 L64,124 L38,157 Z", "M110,157 L150,98 L190,157 L176,157 L150,124 L124,157 Z"],
  },
  ultra: {
    w: 268,
    h: 157,
    d: [
      "M0,0 L50,80 L100,0 L84,0 L50,47 L16,0 Z",
      "M168,0 L218,80 L268,0 L252,0 L218,47 L184,0 Z",
      "M94,32 L127,32 L110.5,62 Z",
      "M141,32 L174,32 L157.5,62 Z",
      "M24,157 L64,98 L104,157 L90,157 L64,124 L38,157 Z",
      "M164,157 L204,98 L244,157 L230,157 L204,124 L178,157 Z",
      "M94,142 L127,142 L110.5,112 Z",
      "M141,142 L174,142 L157.5,112 Z",
    ],
  },
} as const;

export type Tier = keyof typeof ART;

/** Decorative: the tier's name is always written beside it. */
export function TierIcon({ tier, size = 24 }: { tier: Tier; size?: number }) {
  const { w, h, d } = ART[tier];
  const scale = size / 80;
  return (
    <svg
      viewBox={`0 0 ${w} ${h}`}
      width={Math.round(w * scale)}
      height={Math.round(h * scale)}
      aria-hidden="true"
      className="block shrink-0 fill-current text-brand"
    >
      {d.map((path) => (
        <path key={path} d={path} />
      ))}
    </svg>
  );
}

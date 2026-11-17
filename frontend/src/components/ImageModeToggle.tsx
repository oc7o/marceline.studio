import { useImageMode } from "../lib/imageMode";

/*
  The one control that decides whether this app makes an image request at all. Privacy — the
  default, and the state that asks nothing of anyone — reads as the settled teal; load mode, which
  puts real requests on the wire through our passthrough, reads as caution. Colour never works
  alone: the label says which state it is in, in words.
*/

export function ImageModeToggle() {
  const [mode, setMode] = useImageMode();
  const loading = mode === "load";
  return (
    <button
      type="button"
      role="switch"
      aria-checked={loading}
      onClick={() => setMode(loading ? "privacy" : "load")}
      className={`flex items-center gap-2 rounded-full border px-3 py-1.5 text-label transition-colors duration-120 ease-standard ${
        loading
          ? "border-caution-ink bg-caution-quiet text-caution-ink"
          : "border-positive-ink bg-positive-quiet text-positive-ink"
      }`}
      title="Privacy mode loads no images. Load mode fetches posters/frames through our server."
    >
      <span
        aria-hidden="true"
        className={`inline-block h-2.5 w-2.5 rounded-full ${loading ? "bg-caution-ink" : "bg-positive-ink"}`}
      />
      {loading ? "Images: loading" : "Privacy mode: no images"}
    </button>
  );
}

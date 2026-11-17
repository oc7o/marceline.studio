/**
 * Every anchor the app renders, in one listener. The real app mixes `next/link` with plain
 * `<a href="/app/…">` (a document request to the server); a static host has no server, and a
 * document request would also throw away what the visitor changed. So:
 *
 * - an address inside the app is a client navigation;
 * - `/app/api/…` (invoice PDF, notice export, "download my data") runs the app's route handler and
 *   saves its answer as a file;
 * - a sample `.example` address says that it is a sample instead of opening a dead page.
 */
import { say } from "../mock/notify";
import { answerBff, isBffPath } from "./bff";
import { withoutBase } from "./paths";

function filename(response: Response): string {
  return /filename="([^"]+)"/.exec(response.headers.get("content-disposition") ?? "")?.[1] ?? "download";
}

export async function save(url: URL, navigate: (href: string) => void): Promise<void> {
  const response = await answerBff(new Request(url));
  const location = response.headers.get("location");
  if (response.status >= 300 && response.status < 400 && location) {
    const target = new URL(location, url);
    navigate(target.pathname + target.search);
    return;
  }
  if (!response.ok) {
    say("notInDemo");
    return;
  }
  const href = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = href;
  link.download = filename(response);
  link.dataset.demoDownload = "";
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(href), 10_000);
}

export function installLinkHandling(navigate: (href: string) => void): void {
  document.addEventListener("click", (event) => {
    if (event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target instanceof Element ? event.target.closest("a[href]") : null;
    if (!(link instanceof HTMLAnchorElement) || "demoDownload" in link.dataset) return;
    if (link.getAttribute("href")?.startsWith("#")) return;
    let url: URL;
    try {
      url = new URL(link.href);
    } catch {
      return;
    }
    if (url.origin === window.location.origin) {
      if (isBffPath(url.pathname)) {
        event.preventDefault();
        void save(url, navigate);
      } else if (withoutBase(url.pathname) !== null && link.target !== "_blank") {
        event.preventDefault();
        navigate(url.pathname + url.search + url.hash);
      }
    } else if ((url.protocol === "http:" || url.protocol === "https:") && url.hostname.endsWith(".example")) {
      event.preventDefault();
      say("sampleLink");
    }
  });
}

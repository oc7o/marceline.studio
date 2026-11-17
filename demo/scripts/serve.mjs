// A static server that behaves like GitHub Pages: files as they are, a directory's index.html
// (after a 301 to the address with a slash), and dist/404.html (with status 404) for everything
// else. `npm run preview [port]`.
import { createReadStream, existsSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { extname, join, normalize, resolve } from "node:path";

const dist = resolve(import.meta.dirname, "../dist");
const port = Number(process.argv[2] ?? process.env.PORT ?? 4173);

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".svg": "image/svg+xml",
  ".woff2": "font/woff2",
  ".json": "application/json",
  ".txt": "text/plain; charset=utf-8",
};

function fileFor(pathname) {
  const path = join(dist, normalize(decodeURIComponent(pathname)));
  if (!path.startsWith(dist)) return null;
  if (existsSync(path) && statSync(path).isFile()) return path;
  const index = join(path, "index.html");
  return existsSync(index) ? index : null;
}

function isDirectory(pathname) {
  const path = join(dist, normalize(decodeURIComponent(pathname)));
  return path.startsWith(dist) && existsSync(join(path, "index.html")) && statSync(path).isDirectory();
}

createServer((request, response) => {
  const { pathname } = new URL(request.url ?? "/", "http://localhost");
  if (!pathname.endsWith("/") && isDirectory(pathname)) {
    response.writeHead(301, { location: `${pathname}/` }).end();
    return;
  }
  const file = fileFor(pathname);
  const served = file ?? join(dist, "404.html");
  response.writeHead(file ? 200 : 404, { "content-type": TYPES[extname(served)] ?? "application/octet-stream" });
  createReadStream(served).pipe(response);
}).listen(port, "127.0.0.1", () => {
  console.log(`demo/dist on http://127.0.0.1:${port}/`);
});

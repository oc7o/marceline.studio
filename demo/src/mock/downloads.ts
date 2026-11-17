/**
 * The few things the app does with a plain link instead of `fetch`: file downloads from
 * `/api/…` (invoice PDF, notice exports, "download my data") and matched page addresses that
 * open in a new tab. A static host has no such files and the sample sites do not exist, so one
 * click listener answers both — a file built in the browser, or a line saying it is a sample.
 */
import { say } from "./notify";
import {
  type Notice,
  ORGANISATION,
  type Schemas,
  USERNAME,
  findingView,
  findings,
  invoices,
  noticeSubject,
  notices,
  originalView,
  originals,
  removalsView,
  rights,
} from "./state";

interface SampleFile {
  name: string;
  type: string;
  content: string;
}

/** A one-page PDF with the standard Helvetica face: ASCII only, so string offsets are byte offsets. */
function pdf(lines: readonly string[]): string {
  const safe = (line: string) =>
    line
      .replace(/[–—]/g, "-")
      .replace(/[^\x20-\x7e]/g, "?")
      .replace(/[\\()]/g, "\\$&");
  const content = ["BT", "/F1 11 Tf", "16 TL", "56 780 Td", ...lines.map((line) => `(${safe(line)}) Tj T*`), "ET"].join("\n");
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    `<< /Length ${content.length} >>\nstream\n${content}\nendstream`,
  ];
  let out = "%PDF-1.4\n";
  const offsets: number[] = [];
  objects.forEach((object, index) => {
    offsets.push(out.length);
    out += `${index + 1} 0 obj\n${object}\nendobj\n`;
  });
  const xref = out.length;
  out += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  out += offsets.map((offset) => `${String(offset).padStart(10, "0")} 00000 n \n`).join("");
  out += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return out;
}

function euros(cents: number): string {
  return `${(cents / 100).toFixed(2)} EUR`;
}

function invoicePdf(invoice: Schemas["CustomerInvoiceOut"]): SampleFile {
  const cancellation = invoice.kind === "cancellation";
  return {
    name: `${invoice.number}.pdf`,
    type: "application/pdf",
    content: pdf([
      "SAMPLE - archived demo, not a real invoice",
      "",
      `${cancellation ? "Cancellation" : "Invoice"} ${invoice.number}`,
      ...(invoice.cancels_number ? [`Cancels invoice ${invoice.cancels_number}`] : []),
      `Issued on ${invoice.issued_on}`,
      ...(invoice.due_on ? [`Due on ${invoice.due_on}`] : []),
      "",
      `Billed to: ${ORGANISATION.name}`,
      "",
      `Marceline subscription, ${invoice.period_start ?? ""} to ${invoice.period_end ?? ""}`,
      "",
      `Net      ${euros(invoice.net_cents)}`,
      `VAT 19%  ${euros(invoice.vat_cents)}`,
      `Total    ${euros(invoice.gross_cents)}`,
      "",
      `Status: ${invoice.status}${invoice.paid_on ? `, paid on ${invoice.paid_on}` : ""}`,
    ]),
  };
}

function noticeFile(notice: Notice, format: string): SampleFile {
  const body = notice.approved_text ?? notice.preview_text ?? "";
  const stem = `notice-${notice.id.slice(-8)}`;
  if (format === "eml") {
    return {
      name: `${stem}.eml`,
      type: "message/rfc822",
      content: [
        `To: ${notice.recipient_email}`,
        `Subject: ${noticeSubject(notice)}`,
        "X-Unsent: 1",
        "MIME-Version: 1.0",
        "Content-Type: text/plain; charset=utf-8",
        "",
        body,
      ].join("\r\n"),
    };
  }
  if (format === "annex") {
    const rows = notice.findings
      .filter((finding) => finding.included)
      .map((finding) => {
        const row = findings.find((item) => item.page_id === finding.page_id);
        return [
          `Location:      ${finding.url}`,
          `Reviewed on:   ${row?.confirmed_on ?? "—"}`,
          `Last checked:  ${row?.last_checked ?? "—"}`,
          `Extent:        ${finding.extent}`,
          "Basis:         compared with the reference work's stored fingerprints, reviewed by a person and recorded",
        ].join("\n");
      });
    return {
      name: `${stem}-annex.txt`,
      type: "text/plain",
      content: [`Evidence annex — "${notice.work_title}" (${notice.original_id})`, "No image, thumbnail or copy of the reported material is attached.", "", ...rows].join("\n\n"),
    };
  }
  return { name: `${stem}.txt`, type: "text/plain", content: body };
}

function dataExport(): SampleFile {
  return {
    name: "marceline-demo-export.json",
    type: "application/json",
    content: JSON.stringify(
      {
        note: "Sample data from the archived Marceline demo. Nothing in this file is real.",
        account: { username: USERNAME, organisation: ORGANISATION },
        originals: originals.map(originalView),
        findings: findings.map(findingView),
        removals: removalsView(),
        notices,
        rights,
        invoices,
      },
      null,
      2,
    ),
  };
}

function fileFor(path: string): SampleFile | null {
  const invoice = /^\/api\/v1\/app\/invoices\/([^/]+)\/pdf$/.exec(path);
  if (invoice) {
    const found = invoices.find((item) => item.id === invoice[1]);
    return found ? invoicePdf(found) : null;
  }
  const exported = /^\/api\/v1\/app\/notices\/([^/]+)\/export\/(\w+)$/.exec(path);
  if (exported) {
    const found = notices.find((item) => item.id === decodeURIComponent(exported[1] ?? ""));
    return found ? noticeFile(found, exported[2] ?? "txt") : null;
  }
  if (path === "/api/v1/app/export") return dataExport();
  return null;
}

function save(file: SampleFile): void {
  const url = URL.createObjectURL(new Blob([file.content], { type: file.type }));
  const link = document.createElement("a");
  link.href = url;
  link.download = file.name;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

export function installLinkHandling(): void {
  document.addEventListener(
    "click",
    (event) => {
      const link = event.target instanceof Element ? event.target.closest("a[href]") : null;
      if (!(link instanceof HTMLAnchorElement)) return;
      let url: URL;
      try {
        url = new URL(link.href);
      } catch {
        return;
      }
      if (url.origin === window.location.origin && url.pathname.startsWith("/api/")) {
        event.preventDefault();
        const file = fileFor(url.pathname);
        if (file) {
          save(file);
          say("download");
        } else {
          say("notInDemo");
        }
      } else if ((url.protocol === "http:" || url.protocol === "https:") && url.hostname.endsWith(".example")) {
        event.preventDefault();
        say("sampleLink");
      }
    },
    true,
  );
}

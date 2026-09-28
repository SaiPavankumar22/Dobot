// Attachments in the composer: pick, validate, preview.
//
// The limits here mirror the backend's (GET /chat/attachments publishes them). Checking locally is
// not about trusting the client — it is about telling the person *before* a 5 MB upload over a slow
// link that it was never going to be accepted.

import type { AttachmentLimits, ChatAttachment } from "../types";

/** Used until /chat/attachments answers, and when the backend is unreachable. Same numbers. */
export const DEFAULT_LIMITS: AttachmentLimits = {
  images: { max_count: 4, max_bytes: 5 * 1024 * 1024, types: ["image/png", "image/jpeg", "image/webp", "image/gif"] },
  files: { max_count: 4, max_bytes: 256 * 1024, extensions: [".txt", ".md", ".csv", ".json", ".py", ".ts", ".js", ".log"] },
  max_chars_in_prompt: 6000,
  accepted: {
    image_types: ["image/png", "image/jpeg", "image/webp", "image/gif"],
    text_extensions: [".txt", ".md", ".csv", ".json", ".py", ".ts", ".js", ".log"],
  },
};

const FALLBACK_TEXT_EXTENSIONS = [
  ".bat", ".c", ".cfg", ".conf", ".cpp", ".cs", ".css", ".csv", ".env", ".go", ".h", ".hpp", ".htm",
  ".html", ".ini", ".java", ".js", ".json", ".jsonl", ".jsx", ".kt", ".log", ".lua", ".markdown",
  ".md", ".php", ".pl", ".properties", ".ps1", ".py", ".rb", ".rs", ".rst", ".scss", ".sh", ".sql",
  ".swift", ".toml", ".ts", ".tsv", ".tsx", ".txt", ".vue", ".xml", ".yaml", ".yml", ".zsh",
];

export function textExtensions(limits: AttachmentLimits): string[] {
  const known = limits.accepted?.text_extensions ?? [];
  return known.length ? known : FALLBACK_TEXT_EXTENSIONS;
}

/** The `accept` attribute for the file input: what the picker should even offer. */
export function acceptAttribute(limits: AttachmentLimits): string {
  const images = limits.accepted?.image_types ?? DEFAULT_LIMITS.accepted.image_types;
  return [...images, ...textExtensions(limits)].join(",");
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1_048_576).toFixed(1)} MB`;
}

export type Rejection = { name: string; reason: string };

function kindFor(file: File, limits: AttachmentLimits): "image" | "text" | null {
  const mime = (file.type || "").toLowerCase();
  const dot = file.name.lastIndexOf(".");
  const suffix = dot >= 0 ? file.name.slice(dot).toLowerCase() : "";
  const images = limits.accepted?.image_types ?? DEFAULT_LIMITS.accepted.image_types;
  if (images.includes(mime)) return "image";
  if (mime.startsWith("image/")) return null;
  if (mime.startsWith("text/") || mime === "application/json" || mime === "application/xml") return "text";
  if (!mime || mime === "application/octet-stream") {
    if (textExtensions(limits).includes(suffix)) return "text";
  }
  return null;
}

function readAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(reader.error ?? new Error("could not read the file"));
    reader.readAsDataURL(file);
  });
}

/**
 * Validate and read picked files. Returns what was accepted and, per rejected file, why — the
 * composer shows both, because silently dropping a file is the behaviour people hate most.
 */
export async function readAttachments(
  files: File[],
  limits: AttachmentLimits,
  existing: ChatAttachment[] = [],
): Promise<{ attachments: ChatAttachment[]; rejected: Rejection[] }> {
  const accepted: ChatAttachment[] = [];
  const rejected: Rejection[] = [];
  let images = existing.filter((item) => item.kind === "image").length;
  let texts = existing.filter((item) => item.kind === "text").length;

  for (const file of files) {
    const kind = kindFor(file, limits);
    if (kind === null) {
      rejected.push({
        name: file.name,
        reason: `${file.type || "that file type"} is not supported — images (PNG/JPEG/WebP/GIF) or text files only`,
      });
      continue;
    }
    if (kind === "image") {
      if (images >= limits.images.max_count) {
        rejected.push({ name: file.name, reason: `at most ${limits.images.max_count} images per message` });
        continue;
      }
      if (file.size > limits.images.max_bytes) {
        rejected.push({ name: file.name, reason: `${formatBytes(file.size)} — the limit is ${formatBytes(limits.images.max_bytes)} per image` });
        continue;
      }
    } else {
      if (texts >= limits.files.max_count) {
        rejected.push({ name: file.name, reason: `at most ${limits.files.max_count} files per message` });
        continue;
      }
      if (file.size > limits.files.max_bytes) {
        rejected.push({ name: file.name, reason: `${formatBytes(file.size)} — the limit is ${formatBytes(limits.files.max_bytes)} per text file` });
        continue;
      }
    }
    try {
      const dataUrl = await readAsDataUrl(file);
      const comma = dataUrl.indexOf(",");
      accepted.push({
        id: `att_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 7)}`,
        name: file.name || "attachment",
        mime: file.type || (kind === "image" ? "image/png" : "text/plain"),
        kind,
        size: file.size,
        data: comma >= 0 ? dataUrl.slice(comma + 1) : dataUrl,
        previewUrl: kind === "image" ? dataUrl : undefined,
      });
      if (kind === "image") images += 1;
      else texts += 1;
    } catch (error) {
      rejected.push({ name: file.name, reason: error instanceof Error ? error.message : "could not read the file" });
    }
  }
  return { attachments: accepted, rejected };
}

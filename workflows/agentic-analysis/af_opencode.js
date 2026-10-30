// OpenCode plugin of agentic_analysis.py, which sets AF_PLUGIN: workflows/agentic-analysis/README.md.
import fs from "node:fs";
import path from "node:path";

const settings = JSON.parse(process.env.AF_PLUGIN);

const agents = new Map(); // session -> its agent
const parents = new Map(); // session -> the session that started it
const shown = new Map(); // session -> images in its context
const pictures = new Map(); // callID of an image read -> its session
const finished = new Map(); // session -> why its last step ended
const texts = new Map(); // session -> the text of its last step
const replies = new Map(); // session -> the replies OpenCode compacted it after and had it go on from
const compactions = new Set(); // IDs of the compaction parts seen
const endings = new Map(); // session -> whether its last compaction came after the reply that ended its turn
const nudges = new Map(); // ID of OpenCode's prompt to go on after a compaction -> its session's ending then
const summarizing = new Set(); // sessions whose latest request is the compaction agent's
const seated = new Set(); // callIDs of task calls holding a seat
const waiting = [];
let free = settings.seats;

function record(entry) {
  fs.appendFileSync(
    settings.records,
    JSON.stringify({ t: Date.now() / 1000, ...entry }) + "\n",
  );
}

async function sit(callID) {
  if (free > 0) free -= 1;
  // The call that stands up hands its seat over.
  else await new Promise((resolve) => waiting.push(resolve));
  seated.add(callID);
}

function stand(callID) {
  if (!seated.delete(callID)) return;
  const next = waiting.shift();
  if (next) next();
  else free += 1;
}

function head(file) {
  const bytes = Buffer.alloc(12);
  let fd;
  try {
    fd = fs.openSync(file, "r");
    fs.readSync(fd, bytes, 0, bytes.length, 0);
  } catch {
    // A directory or a missing file: the read tool says which.
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
  return bytes;
}

// What OpenCode's read tool would attach rather than read as text.
function kind(file) {
  const bytes = head(file);
  const at = (signature, offset = 0) =>
    signature.every((byte, i) => bytes[offset + i] === byte);
  if (/\.pdf$/i.test(file) || at([0x25, 0x50, 0x44, 0x46])) return "pdf";
  if (
    /\.(png|jpe?g|gif|webp)$/i.test(file) ||
    at([0x89, 0x50, 0x4e, 0x47]) ||
    at([0xff, 0xd8, 0xff]) ||
    at([0x47, 0x49, 0x46, 0x38]) ||
    (at([0x52, 0x49, 0x46, 0x46]) && at([0x57, 0x45, 0x42, 0x50], 8))
  )
    return "image";
  return "text";
}

function isImage(file) {
  return String(file.mime).startsWith("image/");
}

function size(file) {
  try {
    return fs.statSync(file).size;
  } catch {
    return 0;
  }
}

// The bytes of the image a data URL carries.
function decoded(url) {
  return Buffer.byteLength(String(url).split(",")[1] ?? "", "base64");
}

// Why `session` may not take another image of `bytes`, or "" if it may.
function refused(session, bytes) {
  const agent = agents.get(session);
  const cap = settings.images[agent] ?? 0;
  if (cap === 0) {
    const seeing = Object.keys(settings.images).filter(
      (name) => settings.images[name] > 0,
    );
    return `the ${agent} role does not see images. These do: ${seeing.join(", ")}.`;
  }
  if ((shown.get(session) ?? 0) >= cap)
    return (
      `this session holds ${cap} images, as many as one of ${agent} takes. ` +
      "Finish with what you have seen; a fresh session takes further figures."
    );
  const most = settings.image_bytes[agent];
  if (bytes > most)
    return (
      `it is ${bytes} bytes, over the ${most} an image of ${agent} may take. ` +
      "Read a smaller copy: the page, or the figure's PDF, rendered with " +
      "`pdftoppm -r 100 -png`, or a crop of the part to inspect."
    );
  return "";
}

// Throws what the agent is told instead of reading `file`.
function guard(session, callID, file) {
  const what = kind(file);
  if (what === "pdf")
    throw new Error(
      `${file} is a PDF, which no agent reads whole. Render the pages you need with ` +
        `\`pdftoppm -r 150 -png -f FIRST -l LAST ${file} PREFIX\` and read those PNGs, ` +
        "or use `pdftotext` and `pdfinfo` for its text and page count.",
    );
  if (what !== "image") return;
  const why = refused(session, size(file));
  if (why) throw new Error(`${file} was not read: ${why}`);
  shown.set(session, (shown.get(session) ?? 0) + 1);
  pictures.set(callID, session);
}

// The images a request of the session carries: its files, and its tool results' not compacted away.
function carried(messages) {
  let images = 0;
  for (const { parts } of messages)
    for (const part of parts ?? []) {
      if (part.type === "file") images += isImage(part) ? 1 : 0;
      else if (
        part.type === "tool" &&
        part.state?.status === "completed" &&
        !part.state.time?.compacted
      )
        images += (part.state.attachments ?? []).filter(isImage).length;
    }
  return images;
}

// A subagent's task: its session's first message, kept for after a compaction takes it out of the context.
function task(session, messages) {
  const file = path.join(settings.tasks, session);
  if (
    messages.some(({ parts }) =>
      parts?.some((part) => part.type === "compaction"),
    )
  )
    try {
      return fs.readFileSync(file, "utf8");
    } catch {
      return "";
    }
  const text = (messages[0].parts ?? [])
    .filter((part) => part.type === "text" && !part.synthetic)
    .map((part) => part.text)
    .join("\n\n");
  if (text && !fs.existsSync(file)) fs.writeFileSync(file, text);
  return text;
}

// Said instead of OpenCode's prompt to go on after a compaction, or "" to leave it; `given` is a subagent's task.
function nudge(given, ended) {
  const to = given === undefined ? "the human" : "the orchestrator";
  if (ended)
    return (
      "OpenCode summarized this session after your last reply, which ended your " +
      `turn and reaches ${to} as it is. This message does not answer it: end ` +
      "your turn now, in one line."
    );
  if (given === undefined) return "";
  return (
    "OpenCode summarized this session to free its context. Go on with your task " +
    "from where the summary above leaves it, and nothing beyond it, and end with " +
    "your result once it is done: only the orchestrator moves the analysis on.\n\n" +
    (given
      ? `Your task, as the orchestrator gave it:\n\n${given}`
      : "Your task is the one the orchestrator gave you; where the summary above " +
        "records more than that, such as a further phase, step or gate, do none of it.")
  );
}

export default {
  id: "af",
  server: async (input) => {
    fs.mkdirSync(settings.tasks, { recursive: true });
    record({ kind: "loaded", pid: process.pid });
    return {
      "chat.headers": async ({ sessionID, agent }, output) => {
        record({ kind: "beat", session: sessionID });
        if (agent === "compaction") summarizing.add(sessionID);
        else {
          summarizing.delete(sessionID);
          agents.set(sessionID, agent);
        }
        output.headers["x-af-session"] = sessionID;
        output.headers["x-af-agent"] = agent;
        const parent = parents.get(sessionID);
        if (parent) output.headers["x-af-parent"] = parent;
      },
      // Before every request, so a resumed or compacted session counts what it holds.
      "experimental.chat.messages.transform": async (_, { messages }) => {
        const session = messages[0]?.info?.sessionID;
        if (session === undefined) return;
        shown.set(session, carried(messages));
        for (const [callID, owner] of pictures)
          if (owner === session) pictures.delete(callID);
        const given = parents.has(session)
          ? task(session, messages)
          : undefined;
        for (const { parts } of messages)
          for (const part of parts ?? [])
            if (part.metadata?.compaction_continue) {
              const told = nudge(given, nudges.get(part.id));
              if (told) part.text = told;
            }
      },
      "shell.env": async ({ sessionID }, output) => {
        Object.assign(output.env, settings.env);
        output.env.AF_SESSION = sessionID ?? "";
        output.env.AF_AGENT = agents.get(sessionID) ?? "";
      },
      "tool.execute.before": async ({ tool, sessionID, callID }, { args }) => {
        record({ kind: "beat", session: sessionID });
        if (tool === "task") {
          // A resumed session is not created again, and its task grows by what it is resumed with.
          if (args.task_id) {
            parents.set(String(args.task_id), sessionID);
            if (args.prompt)
              fs.appendFileSync(
                path.join(settings.tasks, String(args.task_id)),
                `\n\nThe orchestrator then resumed you with:\n\n${args.prompt}`,
              );
          }
          await sit(callID);
        } else if (tool === "bash" && typeof args.timeout === "number")
          args.timeout = Math.min(args.timeout, settings.timeout);
        else if (tool === "read")
          guard(
            sessionID,
            callID,
            path.resolve(input.directory, String(args.filePath ?? "")),
          );
      },
      // Only a call that succeeds gets here: the events below free the rest.
      "tool.execute.after": async ({ tool, sessionID, callID }, output) => {
        if (tool === "task") {
          stand(callID);
          const child = output.metadata?.sessionId;
          // A task that failed left its replies to the next result of its session.
          for (const reply of (replies.get(child) ?? []).filter(Boolean))
            output.output +=
              "\n\nOpenCode compacted the subagent's session after the reply " +
              "below and had it go on, so the result above is what it said " +
              `after it:\n\n${reply}`;
          replies.delete(child);
          if (finished.get(child) === "length")
            output.output +=
              "\n\nThe subagent's last reply ran out of output tokens, so this " +
              "result is cut short: resume it with its task_id to finish.";
        } else if (tool !== "read" && output.attachments?.length) {
          output.attachments = output.attachments.filter((file) => {
            if (!isImage(file)) return true;
            const why = refused(sessionID, decoded(file.url));
            if (why) output.output += `\n\nAn image was dropped: ${why}`;
            else shown.set(sessionID, (shown.get(sessionID) ?? 0) + 1);
            return !why;
          });
        }
      },
      event: async ({ event }) => {
        const properties = event.properties ?? {};
        if (event.type === "session.created" && properties.info?.parentID) {
          parents.set(properties.info.id, properties.info.parentID);
        } else if (event.type === "message.part.updated") {
          const part = properties.part ?? {};
          const status = part.state?.status;
          const reason = finished.get(part.sessionID);
          if (part.type === "step-start") texts.set(part.sessionID, []);
          else if (part.type === "text" && part.time?.end && !part.synthetic)
            texts.get(part.sessionID)?.push(part.text);
          // Published again after its summary step, with the tail it keeps.
          else if (part.type === "compaction" && !compactions.has(part.id)) {
            compactions.add(part.id);
            const reply = (texts.get(part.sessionID) ?? []).join("\n\n");
            // A compaction after the step that ended a reply, which OpenCode then has go on; one first seen after its summary was begun by an earlier turn, mid-work.
            const ended = Boolean(
              part.auto &&
              !part.overflow &&
              !summarizing.has(part.sessionID) &&
              reason &&
              reason !== "tool-calls" &&
              reason !== "unknown",
            );
            // A reply cut at its output limit, or a last step that said nothing, is not one to end on.
            endings.set(
              part.sessionID,
              ended && reason !== "length" && reply !== "",
            );
            if (ended)
              replies.set(part.sessionID, [
                ...(replies.get(part.sessionID) ?? []),
                reply,
              ]);
          } else if (part.metadata?.compaction_continue && !nudges.has(part.id))
            nudges.set(part.id, endings.get(part.sessionID));
          if (part.type === "step-finish") {
            finished.set(part.sessionID, part.reason);
            if (part.reason === "length" && parents.has(part.sessionID))
              record({
                kind: "cut",
                session: part.sessionID,
                parent: parents.get(part.sessionID),
                agent: agents.get(part.sessionID) ?? null,
              });
          }
          // Pruning a call's output publishes it again.
          if (
            part.type !== "tool" ||
            (status !== "completed" && status !== "error") ||
            part.state.time?.compacted
          )
            return;
          if (part.tool === "task") stand(part.callID);
          const session = pictures.get(part.callID);
          if (session !== undefined && status === "error")
            shown.set(session, shown.get(session) - 1);
          pictures.delete(part.callID);
          record({
            kind: "tool",
            session: part.sessionID,
            parent: parents.get(part.sessionID) ?? null,
            agent: agents.get(part.sessionID) ?? null,
            tool: part.tool,
            status,
            input: JSON.stringify(part.state.input ?? {}).slice(0, 1000),
            error:
              status === "error"
                ? String(part.state.error).slice(0, 1000)
                : null,
          });
        } else if (event.type === "session.error") {
          const session = properties.sessionID ?? null;
          record({
            kind: "error",
            session,
            parent: parents.get(session) ?? null,
            agent: agents.get(session) ?? null,
            error: properties.error?.name ?? null,
            message: String(properties.error?.data?.message ?? "").slice(
              0,
              1000,
            ),
          });
        }
      },
    };
  },
};

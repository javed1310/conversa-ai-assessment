import { Fragment } from "react";
function inline(text: string) {
  return text
    .split(/(\*\*[^*]+\*\*|`[^`]+`)/g)
    .map((part, i) =>
      part.startsWith("**") ? (
        <strong key={i}>{part.slice(2, -2)}</strong>
      ) : part.startsWith("`") ? (
        <code key={i}>{part.slice(1, -1)}</code>
      ) : (
        <Fragment key={i}>{part}</Fragment>
      ),
    );
}
// Deliberately small safe Markdown renderer: no raw HTML, remote embeds or script URLs.
export function Markdown({ text }: { text: string }) {
  return (
    <div className="markdown">
      {text.split(/\n\s*\n/).map((block, i) => {
        if (/^### /.test(block))
          return <h4 key={i}>{inline(block.slice(4))}</h4>;
        if (/^## /.test(block))
          return <h3 key={i}>{inline(block.slice(3))}</h3>;
        if (/^# /.test(block)) return <h2 key={i}>{inline(block.slice(2))}</h2>;
        if (/^[-*] /.test(block))
          return (
            <ul key={i}>
              {block.split("\n").map((line, j) => (
                <li key={j}>{inline(line.replace(/^[-*] /, ""))}</li>
              ))}
            </ul>
          );
        return <p key={i}>{inline(block)}</p>;
      })}
    </div>
  );
}

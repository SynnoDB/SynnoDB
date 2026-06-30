'use strict';

// ── Minimal syntax highlighter for the code inspector ────────────────────
// Dependency-free, same spirit as the Markdown renderer in util.js. Picks a
// language from the file extension and wraps lexical tokens in
// <span class="tok-…"> elements. Token text is HTML-escaped via esc() before
// it reaches the DOM, so highlighting never introduces unescaped markup;
// unknown extensions fall back to plain escaped text.

// Scan `src` left-to-right against an ordered list of [class, sticky-regex]
// rules. The first rule that matches at the cursor wins; anything no rule
// claims is emitted as an escaped literal character. Every regex MUST carry
// the sticky flag (/…/y) so it only matches at the current cursor position.
function tokenize(src, rules) {
  let out = '';
  let i = 0;
  const n = src.length;
  while (i < n) {
    let matched = false;
    for (const [cls, re] of rules) {
      re.lastIndex = i;
      const m = re.exec(src);
      if (m && m[0]) {
        out += cls ? `<span class="tok-${cls}">${esc(m[0])}</span>` : esc(m[0]);
        i += m[0].length;
        matched = true;
        break;
      }
    }
    if (!matched) { out += esc(src[i]); i++; }
  }
  return out;
}

// ── C / C++ ──────────────────────────────────────────────────────────────
const _CPP_KEYWORDS =
  'alignas alignof and and_eq asm auto bitand bitor break case catch class compl ' +
  'concept const consteval constexpr constinit const_cast continue co_await ' +
  'co_return co_yield decltype default delete do dynamic_cast else enum explicit ' +
  'export extern final for friend goto if inline mutable namespace new noexcept ' +
  'not not_eq operator or or_eq override private protected public register ' +
  'reinterpret_cast requires return sizeof static static_assert static_cast struct ' +
  'switch template this thread_local throw try typedef typeid typename union using ' +
  'virtual volatile while xor xor_eq';

const _CPP_TYPES =
  'bool char char8_t char16_t char32_t double float int long short signed unsigned ' +
  'void wchar_t size_t ssize_t ptrdiff_t int8_t int16_t int32_t int64_t uint8_t ' +
  'uint16_t uint32_t uint64_t string string_view wstring vector map unordered_map ' +
  'set unordered_set array pair tuple optional variant span';

const _CPP_LITERALS = 'true false nullptr NULL';

const _wordRe = words => new RegExp('\\b(?:' + words.trim().split(/\s+/).join('|') + ')\\b', 'y');

const CPP_RULES = [
  ['comment', /\/\/[^\n]*|\/\*[\s\S]*?\*\//y],
  ['pre',     /^[ \t]*#[^\n]*/my],                                    // preprocessor directive
  ['str',     /R"([^()\\ ]{0,16})\([\s\S]*?\)\1"/y],                 // raw string literal
  ['str',     /"(?:\\.|[^"\\\n])*"?/y],                              // string literal
  ['char',    /'(?:\\.|[^'\\\n])*'/y],                               // char literal
  ['num',     /\b(?:0[xX][0-9a-fA-F']+|0[bB][01']+|\d[\d']*(?:\.[\d']*)?(?:[eE][+-]?\d+)?)[uUlLfF]*\b|\.\d[\d']*(?:[eE][+-]?\d+)?[fF]?/y],
  ['lit',     _wordRe(_CPP_LITERALS)],
  ['kw',      _wordRe(_CPP_KEYWORDS)],
  ['type',    _wordRe(_CPP_TYPES)],
  ['fn',      /[A-Za-z_]\w*(?=\s*\()/y],                             // call / declaration name
  ['ident',   /[A-Za-z_]\w*/y],
];

// ── Markdown ─────────────────────────────────────────────────────────────
// Highlights Markdown *source* (so the reader sees the raw syntax, coloured),
// rather than rendering it to HTML. Fenced code blocks are tracked across
// lines; everything else is highlighted line by line.
const MD_INLINE_RULES = [
  ['md-code',   /`[^`\n]+`/y],
  ['md-bold',   /\*\*[^*\n]+\*\*|__[^_\n]+__/y],
  ['md-italic', /\*[^*\s][^*\n]*\*|(?<![A-Za-z0-9])_[^_\s][^_\n]*_(?![A-Za-z0-9])/y],
  ['md-link',   /!?\[[^\]\n]*\]\([^)\n]*\)/y],
];

const MD_LINE_RULES = [
  ['md-heading', /^\s{0,3}#{1,6}\s.*$/y],
  ['md-hr',      /^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/y],
  ['md-quote',   /^\s{0,3}>+/y],
  ['md-list',    /^\s*(?:[-*+]|\d+\.)\s/y],
  ...MD_INLINE_RULES,
];

function highlightMarkdown(src) {
  const out = [];
  let inFence = false;
  for (const line of String(src).replace(/\r\n?/g, '\n').split('\n')) {
    if (/^\s{0,3}(```|~~~)/.test(line)) {
      inFence = !inFence;
      out.push(`<span class="tok-md-fence">${esc(line)}</span>`);
    } else if (inFence) {
      out.push(esc(line));
    } else {
      out.push(tokenize(line, MD_LINE_RULES));
    }
  }
  return out.join('\n');
}

// ── Dispatch ─────────────────────────────────────────────────────────────
const _CPP_EXTS = new Set(
  ['cpp', 'cc', 'cxx', 'c++', 'c', 'hpp', 'hh', 'hxx', 'h++', 'h', 'ipp', 'tpp', 'inl', 'cu', 'cuh']);
const _MD_EXTS = new Set(['md', 'markdown', 'mdown', 'mkd']);

function highlightCode(path, content) {
  const ext = (String(path).split('.').pop() || '').toLowerCase();
  if (_CPP_EXTS.has(ext)) return tokenize(content, CPP_RULES);
  if (_MD_EXTS.has(ext))  return highlightMarkdown(content);
  return esc(content);
}

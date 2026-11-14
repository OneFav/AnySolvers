"""Find short advisory excerpts in the current domain's Markdown topic pages."""
import re

from tools import common as c


def _paragraphs(body):
    """Return Markdown paragraphs, keeping headings and footnote definitions separate."""
    lines = []
    for line in body.splitlines():
        if not line.strip() or line.startswith(('#', '[^')):
            if lines:
                yield ' '.join(lines)
                lines.clear()
            if line.strip():
                yield line.strip()
        else:
            lines.append(line.strip())
    if lines:
        yield ' '.join(lines)


def _matches(page, body, terms):
    title = page.stem
    section = None
    notes = {}
    paragraphs = []
    speculative = page.name.startswith('无证据猜想-')
    sections = ('无证据猜想',) if speculative else ('有证据的发现', '待验证的猜想')
    for block in _paragraphs(body):
        if block.startswith('# '):
            title = block[2:].strip()
            section = None
        elif block.startswith('## '):
            section = block[3:].strip()
        elif footnote := re.fullmatch(r'\[\^([^\]]+)\]:\s*(.*)', block):
            notes[footnote[1]] = footnote[2]
        elif not block.startswith('#') and section in sections:
            paragraphs.append((section, block))

    title_score = 2 * sum(term in title.casefold() for term in terms)
    for section, paragraph in paragraphs:
        score = title_score + sum(term in paragraph.casefold() for term in terms)
        if score:
            refs = dict.fromkeys(re.findall(r'\[\^([^\]]+)\]', paragraph))
            yield score, dict(page='dream/wiki/topics/' + page.name, title=title,
                              section=section, excerpt=paragraph[:800],
                              sources=[] if speculative else [notes[r] for r in refs if r in notes])


def excerpts(query):
    terms = set(re.findall(r'\w+', query.casefold()))
    if not terms:
        return []
    try:
        topics = c.contained(c.ROOT, 'dream/wiki/topics')
        pages = sorted(topics.glob('*.md'))
    except (OSError, ValueError):
        return []
    matches = []
    for page in pages:
        if page.is_symlink() or not page.is_file():
            continue
        try:
            body = page.read_text(encoding='utf-8')
        except (OSError, UnicodeError):
            continue
        matches.extend(_matches(page, body, terms))
    matches.sort(key=lambda item: -item[0])
    return [entry for _, entry in matches[:3]]

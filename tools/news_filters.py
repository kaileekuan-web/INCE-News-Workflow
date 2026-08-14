#!/usr/bin/env python3
"""
Shared news-quality filters for the AI news pipeline.

One home for the four rules the general news report is built on:

  1. **Startups only.** Frontier labs and big tech (frontier_labs.txt) are
     dropped. They are covered everywhere else; the report exists for the
     smaller companies that aren't.
  2. **News, not opinion.** A post has to report an event — a launch, a raise,
     an acquisition, a result — rather than react to one.
  3. **No duplicates.** Two accounts posting the same story, a thread head and
     its standalone version, and two links to the same article are one item.
  4. **Link to the news, not to X.** x.com links are provenance, not sources;
     the report links to the article the post points at.

These live here rather than in collect_x.py because they run at three different
points: at collection (cheapest place to drop a post — every survivor costs an
LLM call downstream), and again at document generation (so an existing .tmp
file, or an article added by hand, gets the same treatment).

Self-test:  python tools/news_filters.py
"""

import os
import re
import sys
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LABS_FILE = os.path.join(REPO_ROOT, 'frontier_labs.txt')

# Running this file directly (`python tools/news_filters.py`, which is how the
# self-tests run) puts tools/ on sys.path, not the repo root — so `tools.grounding`
# below would not resolve. It failed silently for exactly one commit: figures
# stopped counting as dedup markers, and the only symptom was one self-test
# disagreeing with the same code called through an import.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Figure normalization is grounding's job — it already knows "$50 million" and
# "5000万美元" are one number. Imported at module level so a broken import is an
# error here rather than a quiet loss of dedup accuracy at runtime.
from tools.grounding import _iter_figures, _is_checkable


# ── Vocabulary ────────────────────────────────────────────────────────────────
# collect_x.py imports these; keeping one copy means a word added for the
# collector is also seen by the document generator.

AI_KEYWORDS = [
    'ai', 'artificial intelligence', 'machine learning', 'gpt', 'llm',
    'agent', 'model', 'openai', 'anthropic', 'claude', 'gemini', 'llama',
    'mistral', 'neural', 'inference', 'training', 'gpu', 'diffusion',
]

# Words that mark a post as reporting something that happened, rather than
# opining about it.
NEWS_SIGNALS = [
    # launches / releases
    'launch', 'launching', 'launched', 'release', 'releasing', 'released',
    'introduc', 'announc', 'unveil', 'ship', 'shipping', 'shipped',
    'available', 'now live', 'rolling out', 'rolled out', 'general availability',
    'open source', 'open-source', 'open sourcing', 'preview', 'beta',
    # money
    'raise', 'raised', 'raising', 'funding', 'round', 'seed', 'series a',
    'series b', 'series c', 'valuation', 'acquir', 'acquisition', 'merger',
    'ipo', 'invest', 'backed by', 'led by',
    # company / people moves
    'joining', 'joined', 'hiring', 'appointed', 'stepping down', 'partnership',
    'partnering', 'collaborat', 'deal with', 'contract',
    # results
    'benchmark', 'state of the art', 'sota', 'outperform', 'record',
    'results', 'paper', 'research', 'study', 'report',
]

# Chinese equivalents, so a WeChat article isn't read as opinion just because
# the English vocabulary above finds nothing in it.
NEWS_SIGNALS_ZH = [
    '发布', '推出', '上线', '宣布', '开源', '亮相', '正式', '首发',
    '融资', '轮', '估值', '收购', '并购', '投资', '领投', '跟投', '上市',
    '合作', '签署', '达成', '任命', '加入', '离职', '成立',
    '测评', '基准', '论文', '研究', '报告', '数据显示',
]

# Pure calls-to-action carry no summarizable content even when long enough.
PROMO_MARKERS = [
    'link in bio', 'sign up', 'sign-up', 'register now', 'rsvp', 'join us',
    'tickets', 'apply now', 'waitlist', 'giveaway', 'retweet to', 'follow us',
    'subscribe', 'we are hiring', "we're hiring",
]

# Phrases that mark a post as the author's take on the news rather than the
# news. These are matched as substrings — "imo" needs word boundaries (it is
# inside "impose"), which _phrase_re handles for every entry.
OPINION_MARKERS = [
    # first person framing
    'i think', 'i believe', 'i suspect', 'i predict', 'i expect', 'i feel',
    "i'm convinced", 'im convinced', "i've been saying", 'ive been saying',
    'my take', 'my view', 'my guess', 'my prediction', 'my bet',
    'in my view', 'in my opinion', 'imo', 'imho', 'personally',
    'the way i see it', 'if you ask me', 'i would argue', "i'd argue",
    # take framing
    'hot take', 'unpopular opinion', 'controversial opinion', 'spicy take',
    'here is why', "here's why", 'why i think', 'the real reason',
    'nobody is talking about', 'no one is talking about',
    'change my mind', 'prove me wrong', 'mark my words',
    'prediction:', 'thesis:', 'take:', 'reminder that', 'friendly reminder',
    # engagement bait / discussion prompts
    'thoughts?', 'agree?', 'am i wrong', 'what do you think',
    'underrated', 'overrated', 'the future belongs to',
    # Chinese
    '我认为', '我觉得', '个人观点', '一点思考', '我的判断', '值得深思',
    '你怎么看', '不吐不快',
]


# Wording that marks an item as somebody SAYING something rather than something
# HAPPENING. A CEO's remarks at a conference, an interview, a town hall, a
# prediction from a founder — all real, all sourced, none of them events.
#
# These only disqualify an item when it carries no independent event of its own:
# "Sierra's CEO said the company raised $350M" is a funding round being reported
# through a quote, and belongs in the report. See is_statement().
STATEMENT_MARKERS = [
    # attribution
    'said', 'says', 'saying', 'told', 'telling', 'tells', 'stated', 'states',
    'commented', 'remarked', 'noted that', 'according to', 'quoted',
    'in an interview', 'interview with', 'speaking at', 'spoke at',
    'on stage at', 'at a conference', 'keynote', 'fireside', 'town hall',
    'on the podcast', 'podcast with', 'ama', 'q&a', 'asked about',
    # what executives do in these items
    'believes', 'expects', 'predicts', 'predicted', 'warns', 'warned',
    'argues', 'argued', 'claims', 'insists', 'suggests', 'urges',
    'doubles down', 'pushes back', 'responds to', 'weighs in', 'reacts to',
    'addressed', 'downplayed', 'dismissed',
    # Chinese
    '表示', '称', '说道', '透露', '回应', '认为', '强调', '指出', '谈到',
    '接受采访', '演讲', '专访', '发声', '喊话',
]

# Event vocabulary strong enough to redeem a statement-shaped item: if one of
# these is present, something concrete happened and the quote is just how it was
# reported. Narrower than NEWS_SIGNALS on purpose — 'report' and 'research'
# appear in half of all commentary, so they are not on this list.
HARD_EVENT_SIGNALS = [
    'raise', 'raised', 'raising', 'funding round', 'series a', 'series b',
    'series c', 'series d', 'seed round', 'valuation', 'acquir', 'acquisition',
    'merger', 'ipo', 'launch', 'launched', 'launches', 'released', 'releases',
    'shipping', 'shipped', 'unveiled', 'introduced', 'announced', 'rolling out',
    'now available', 'general availability', 'open sourced', 'partnership with',
    'appointed', 'stepping down', 'resigned', 'hired', 'laid off', 'layoffs',
    'sued', 'lawsuit', 'fined', 'banned',
    '融资', '收购', '并购', '上市', '发布', '推出', '上线', '开源', '任命', '离职',
]


def _phrase_re(phrases, prefix_boundary=r'(?<![A-Za-z0-9_])',
               suffix_boundary=r'(?![A-Za-z0-9_-])'):
    """
    Compile an alternation that matches any phrase at a token boundary.

    \\b is not usable directly: several entries end in punctuation ('prediction:',
    'thoughts?') or contain it ('x.ai', 'dall·e'), and \\b after ':' means
    "followed by a word character", which is the opposite of what we want.

    The trailing hyphen in the default suffix guard is deliberate: it keeps
    "Google-backed startup raises $20M" out of the Google bucket, which is a
    startup story, while "Google released…" still matches.

    Longest phrase first, so "meta superintelligence" wins over "meta" and the
    reported reason names the specific thing that matched.
    """
    if not phrases:
        return re.compile(r'(?!)')      # never matches
    body = '|'.join(re.escape(p) for p in sorted(phrases, key=len, reverse=True))
    return re.compile(f'{prefix_boundary}(?:{body}){suffix_boundary}', re.I)


_AI_RE = re.compile(
    r'\b(?:' + '|'.join(re.escape(k) for k in AI_KEYWORDS) + r')s?\b', re.I)
_NEWS_RE = re.compile(
    r'\b(?:' + '|'.join(re.escape(k) for k in NEWS_SIGNALS) + r')', re.I)
_NEWS_ZH_RE = re.compile('|'.join(re.escape(k) for k in NEWS_SIGNALS_ZH))
_OPINION_RE = re.compile(
    r'(?<![A-Za-z0-9_])(?:' + '|'.join(
        re.escape(p) for p in sorted(OPINION_MARKERS, key=len, reverse=True)) + r')',
    re.I)
_STATEMENT_RE = _phrase_re(STATEMENT_MARKERS)
_HARD_EVENT_RE = re.compile(
    r'(?:' + '|'.join(re.escape(k) for k in HARD_EVENT_SIGNALS) + r')', re.I)

# A concrete dollar figure is itself an event marker. Funding posts often skip
# the verb entirely ("Base, Valar and Hadrian get $1B+ each").
_MONEY_RE = re.compile(
    r'[$€£]\s?\d[\d,.]*\s?(?:k|m|b|bn|mm|million|billion)?\+?\b|\d+\s?(?:亿|万)元?', re.I)


def is_ai_relevant(text: str) -> bool:
    """True if the post actually mentions AI (whole words, optional plural)."""
    return bool(_AI_RE.search(text or ''))


def is_newsworthy(text: str) -> bool:
    """True if the text reports an event rather than commenting on one."""
    text = text or ''
    return bool(_NEWS_RE.search(text) or _NEWS_ZH_RE.search(text)
                or _MONEY_RE.search(text))


def is_promo(text: str) -> bool:
    low = (text or '').lower()
    return any(k in low for k in PROMO_MARKERS)


def opinion_marker(text: str) -> str:
    """Return the opinion phrase found in `text`, or '' if it reads as reporting."""
    m = _OPINION_RE.search(text or '')
    return m.group(0).lower() if m else ''


def has_hard_event(text: str) -> bool:
    """True if the text reports a concrete, checkable event (raise, launch, …)."""
    return bool(_HARD_EVENT_RE.search(text or ''))


def is_statement(text: str) -> tuple:
    """
    Is this an executive saying something, rather than something happening?

    Returns (is_statement, reason).

    "Zuckerberg told staff AI agents haven't progressed as fast as he hoped" is
    a real, sourced, dateable item — and it is a quote, not an event. The report
    is for events, so it goes.

    The redemption clause is what keeps this from eating the report: an item
    that also carries a hard event ("the CEO said the company raised $350M")
    is a funding round reported through a quote, and stays.
    """
    m = _STATEMENT_RE.search(text or '')
    if not m:
        return False, ''
    if has_hard_event(text):
        return False, ''
    return True, f"statement ('{m.group(0).lower()}')"


def is_opinion(text: str, require_news_signal: bool = False) -> tuple:
    """
    Decide whether a piece of text is something other than a reported event.

    Returns (not_news, reason). Three tests, because they fail differently:

      - An explicit opinion marker ("my take", "unpopular opinion") is decisive
        on its own, even in a post that also reports a fact — those posts are
        a reaction to news, and the news itself will have been collected from
        the source that broke it.
      - A statement ("the CEO said…", "speaking at…") is decisive unless the
        item also carries a hard event — see is_statement().
      - Absence of any event vocabulary is only suggestive, so it is opt-in
        (`require_news_signal`). It is right for a VC's timeline and wrong for
        a WeChat article, whose vocabulary this module only partly covers.
    """
    marker = opinion_marker(text)
    if marker:
        return True, f"opinion marker '{marker}'"
    statement, reason = is_statement(text)
    if statement:
        return True, reason
    if require_news_signal and not is_newsworthy(text or ''):
        return True, 'no event reported'
    return False, ''


# ── Frontier labs ─────────────────────────────────────────────────────────────

# How much of the opening counts as "the subject of the story". A lab named in
# the first clause is what the post is about; further in it is context
# ("...to compete with OpenAI", "founded by ex-DeepMind researchers"), and
# those are startup stories we want to keep.
SUBJECT_ZONE_CHARS = 70

# Wording that turns a lab name into background rather than the subject. The
# single most common shape in startup news is the ex-lab founder — "founded by
# two former DeepMind researchers" — followed by the competitor framing
# ("to take on Gemini") and the built-on framing ("powered by GPT-5"). Every one
# of those is a startup story that names a lab early, so the name alone can't be
# the test.
CONTEXT_BEFORE = [
    'ex', 'ex-', 'former', 'formerly', 'previously', 'veteran', 'veterans',
    'alum', 'alumni', 'alumnus', 'left', 'leaving', 'departed', 'poached',
    'founded by', 'co-founded by', 'started by', 'spun out of', 'spinout from',
    'backed by', 'funded by', 'invested', 'investor in', 'partner with',
    'partnered with', 'partnership with', 'deal with', 'customer of',
    'compete with', 'competing with', 'competitor to', 'rival', 'rivals',
    'take on', 'takes on', 'taking on', 'challenge to', 'alternative to',
    'built on', 'build on', 'powered by', 'running on', 'using', 'via',
    'than', 'vs', 'vs.', 'versus', 'unlike', 'beats', 'beat', 'outperforms',
    'ahead of', 'behind',
]
_CONTEXT_BEFORE_RE = re.compile(
    r'(?:' + '|'.join(re.escape(c) for c in sorted(CONTEXT_BEFORE, key=len, reverse=True))
    + r')[\s\-–—,]*$', re.I)

# How far back to look for that wording. Long enough for "founded by two former
# <lab>", short enough that an unrelated earlier clause doesn't excuse a lab
# that really is the subject.
CONTEXT_LOOKBACK_CHARS = 45


class Lab:
    """One excluded company: display name plus the three ways we recognise it."""

    __slots__ = ('name', 'aliases', 'handles', 'domains', '_alias_re')

    def __init__(self, name, aliases, handles, domains):
        self.name = name
        self.aliases = aliases
        self.handles = {h.lower().lstrip('@') for h in handles}
        self.domains = {d.lower().lstrip('.') for d in domains}
        self._alias_re = _phrase_re(aliases) if aliases else None

    def subject_alias(self, text: str):
        """
        The alias that names this lab as the story's subject, or None.

        Every occurrence in `text` is checked: an alias introduced by context
        wording ("former DeepMind", "to take on Gemini") is skipped, and the
        search continues — a post can mention a lab as background and still be
        about that lab later in the same clause.
        """
        if not self._alias_re or not text:
            return None
        for m in self._alias_re.finditer(text):
            before = text[max(0, m.start() - CONTEXT_LOOKBACK_CHARS):m.start()]
            if _CONTEXT_BEFORE_RE.search(before):
                continue
            return m.group(0)
        return None


_DOMAIN_RE = re.compile(r'^[a-z0-9-]+(?:\.[a-z0-9-]+)+$', re.I)


def load_labs(path: str = None) -> list:
    """
    Parse frontier_labs.txt into Lab objects.

    Missing file is not an error — it means "exclude nothing", which is a
    legitimate configuration and keeps the pipeline running on a fresh checkout.
    """
    path = path or DEFAULT_LABS_FILE
    if not os.path.exists(path):
        return []

    labs = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or ':' not in line:
                continue
            name, _, rest = line.partition(':')
            aliases, handles, domains = [], [], []
            for token in (t.strip() for t in rest.split(',')):
                if not token:
                    continue
                if token.startswith('@'):
                    handles.append(token)
                elif _DOMAIN_RE.match(token) and ' ' not in token:
                    domains.append(token)
                    # A bare domain is also a usable alias ("mistral.ai" in prose)
                else:
                    aliases.append(token)
            labs.append(Lab(name.strip(), aliases, handles, domains))
    return labs


_LABS_CACHE = {}


def get_labs(path: str = None) -> list:
    """Cached load_labs — the filters are called once per article."""
    key = path or DEFAULT_LABS_FILE
    if key not in _LABS_CACHE:
        _LABS_CACHE[key] = load_labs(key)
    return _LABS_CACHE[key]


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or '').lower().lstrip('.')
    except ValueError:
        return ''


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith('.' + domain)


def article_text(article: dict) -> str:
    """The post's own words — tweet text or article body, without the handle prefix."""
    text = (article.get('description') or article.get('content')
            or article.get('title') or '')
    return re.sub(r'^@[A-Za-z0-9_]+:\s*', '', text).strip()


def frontier_match(article: dict, labs: list = None) -> tuple:
    """
    Is this story about a frontier lab / big tech company?

    Returns (lab_name, reason) or (None, '').

    Three tests, deliberately ordered from certain to heuristic:

      author — the post is BY the lab. Its own announcement; certain.
      domain — the link goes to the lab's own site. Also certain.
      subject — a lab name opens the post. Strong, but not certain, which is
                why it only looks at the first SUBJECT_ZONE_CHARS characters.
                "Cursor raises $200M to take on GitHub Copilot" survives this;
                "OpenAI ships GPT-5.6" does not.

    Anything subtler than that (a story that is really about OpenAI without
    naming it early) is left to the `subject_type` field the summarizer
    assigns — an LLM reading the whole article beats a regex reading its lead.
    """
    labs = labs if labs is not None else get_labs()
    if not labs:
        return None, ''

    author = (article.get('author') or '').lower().lstrip('@')
    hosts = [_host(u) for u in (article.get('source_url'), article.get('url'))
             if u]
    hosts = [h for h in hosts if h and not is_x_url('https://' + h)]

    body = article_text(article)
    subject_zone = body[:SUBJECT_ZONE_CHARS]

    for lab in labs:
        if author and author in lab.handles:
            return lab.name, f'posted by @{author}'
        for host in hosts:
            for domain in lab.domains:
                if _host_matches(host, domain):
                    return lab.name, f'link to {domain}'
        alias = lab.subject_alias(subject_zone)
        if alias:
            return lab.name, f"'{alias}' opens the story"
    return None, ''


def is_frontier(article: dict, labs: list = None) -> bool:
    return frontier_match(article, labs)[0] is not None


# The category the summarizer assigns when a story turns out to have nothing to
# do with AI. It is the backstop for posts that entered collection on a funding
# event alone (see collect_x.passes_filter, `topic-unverified`).
NON_AI_CATEGORY = '非AI'

# Values the summarizer may assign to `subject_type`; both are excluded from the
# general news report.
EXCLUDED_SUBJECT_TYPES = ('frontier_lab', 'big_tech')

# `content_type` values that are not reported events, and the reason shown in
# the run log. Only 'news' survives.
EXCLUDED_CONTENT_TYPES = {
    'opinion': 'commentary, not news',
    'statement': 'a statement, not an event',
}


def excluded_by_summary(article: dict) -> str:
    """
    Reason to drop an article, or '' to keep it, from the summarizer's reading
    of it — `subject_type` (who the story is about) and `content_type` (whether
    it is a story at all).

    When `content_type` is missing the judgement falls back to the keyword test,
    requiring some event vocabulary. That case is not hypothetical: articles
    summarized before these fields existed, and anything added to the JSON by
    hand, arrive here unclassified, and without the fallback a timeline of
    World Cup posts would sail into the report unexamined.
    """
    # Posts kept at collection on the strength of a funding event, without any
    # AI wording, land here to be judged by a reader of the whole article. A VC
    # posting a biotech round is exactly what this catches.
    if article.get('category') == NON_AI_CATEGORY:
        return 'not an AI company'
    if article.get('subject_type') in EXCLUDED_SUBJECT_TYPES:
        return f"subject is {article['subject_type'].replace('_', ' ')}"
    content_type = article.get('content_type')
    if content_type in EXCLUDED_CONTENT_TYPES:
        return EXCLUDED_CONTENT_TYPES[content_type]
    if not content_type:
        text = article.get('title', '') + ' ' + article_text(article)
        not_news, reason = is_opinion(text, require_news_signal=True)
        if not_news:
            if 'marker' in reason:
                return 'commentary, not news'
            if reason.startswith('statement'):
                return 'a statement, not an event'
            return 'no event reported'
    return ''


# ── Links ─────────────────────────────────────────────────────────────────────

_URL_RE = re.compile(r'https?://[^\s<>"\')\]]+', re.I)
_X_HOSTS = ('x.com', 'twitter.com', 'mobile.twitter.com', 'nitter.net',
            'xcancel.com', 'lightbrd.com')
_SHORTENER_HOSTS = ('t.co', 'bit.ly', 'buff.ly', 'ow.ly', 'lnkd.in',
                    'trib.al', 'dlvr.it', 'shorturl.at', 'tinyurl.com')
# Tracking parameters that make two links to the same article look different.
_TRACKING_PARAMS_RE = re.compile(
    r'^(utm_|ref_?$|ref_src|ref_url|fbclid|gclid|mc_cid|mc_eid|s$|t$|si$|cmpid|smid)',
    re.I)


def is_x_url(url: str) -> bool:
    """True for x.com/twitter.com links and the Nitter mirrors of them."""
    host = _host(url)
    if not host:
        return False
    host = host[4:] if host.startswith('www.') else host
    return any(host == h or host.endswith('.' + h) for h in _X_HOSTS)


def is_shortener(url: str) -> bool:
    host = _host(url)
    host = host[4:] if host.startswith('www.') else host
    return host in _SHORTENER_HOSTS


def canonical_url(url: str) -> str:
    """
    Normalize a URL for comparison: https, lowercase host, no www/m/amp prefix,
    no tracking parameters, no fragment, no trailing slash or /amp suffix.

    Used as a dedup key — two posts linking to the same article are the same
    news whether the link arrived with a utm campaign, over http, through the
    mobile host, or as the AMP copy. Each of those spellings is one duplicate
    that would otherwise reach the report.
    """
    if not url:
        return ''
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    host = (parts.hostname or '').lower()
    for prefix in ('www.', 'm.', 'amp.', 'mobile.'):
        if host.startswith(prefix):
            host = host[len(prefix):]
            break
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if not _TRACKING_PARAMS_RE.match(k)])
    path = parts.path.rstrip('/')
    for suffix in ('/amp', '/index.html', '/index.htm'):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    # Scheme is normalized rather than preserved: http and https spellings of
    # one article are the same article, and comparing them as different was a
    # silent source of duplicates.
    return urlunsplit(('https', host, path, query, ''))


def expand_url(url: str, timeout: float = 6.0, _cache={}) -> str:
    """
    Resolve a shortened link to its destination. Returns the input unchanged on
    any failure — a dead t.co is still better data than an empty field.
    """
    if not url or not is_shortener(url):
        return url
    if url in _cache:
        return _cache[url]
    resolved = url
    try:
        import requests
        resp = requests.head(url, allow_redirects=True, timeout=timeout,
                             headers={'User-Agent': 'Mozilla/5.0'})
        if resp.url:
            resolved = resp.url
        if resp.status_code >= 400:
            # Some hosts refuse HEAD; one cheap GET before giving up.
            resp = requests.get(url, allow_redirects=True, timeout=timeout,
                                stream=True, headers={'User-Agent': 'Mozilla/5.0'})
            if resp.url:
                resolved = resp.url
            resp.close()
    except Exception:
        pass
    _cache[url] = resolved
    return resolved


def extract_source_url(text: str, expand: bool = True) -> str:
    """
    The link to the actual news inside a post, or '' if the post links nowhere.

    Skips x.com/Nitter links (that's the post itself, not its source) and
    resolves shorteners so the domain is visible to the frontier-lab filter and
    to dedup. The first surviving link wins: when a post links to both the
    announcement and a signup page, the announcement comes first in practice.
    """
    if not text:
        return ''
    for raw in _URL_RE.findall(text):
        url = raw.rstrip('.,;:!?)]}’"\'')
        if is_x_url(url):
            continue
        if expand and is_shortener(url):
            url = expand_url(url)
            if is_x_url(url):
                continue
        return url
    return ''


def news_link(article: dict) -> str:
    """
    The URL a report should link to: the news behind the post, never x.com.

    Order: the source link extracted at collection time, then the article's own
    URL if it isn't an X link, then '' — which renders as an unlinked headline
    rather than a link back to X.

    Prefers a primary source over an aggregator when the article carries both —
    see best_source_link(), which this delegates to. Kept as the name every
    caller already uses.
    """
    return best_source_link(article)


# ── Primary sources over aggregators ──────────────────────────────────────────

# News aggregators and syndicators: sites that report on someone else's
# announcement. They are perfectly good sources, but when the company's own
# launch post or the investor's own announcement exists, that is what the report
# should link to — the aggregator's writeup is a summary of it, one hop further
# from the facts, and often paywalled or rewritten.
#
# This is a de-prioritization, not a ban: an aggregator link is still used when
# it is the only link there is. A story no primary source covers is still a
# story.
AGGREGATOR_HOSTS = (
    # tech trade press
    'techcrunch.com', 'theverge.com', 'venturebeat.com', 'engadget.com',
    'arstechnica.com', 'wired.com', 'zdnet.com', 'cnet.com', 'gizmodo.com',
    'thenextweb.com', 'mashable.com', 'techradar.com', 'digitaltrends.com',
    'siliconangle.com', 'protocol.com', 'axios.com', 'theinformation.com',
    'businessinsider.com', 'forbes.com', 'fortune.com', 'cnbc.com',
    'bloomberg.com', 'reuters.com', 'ft.com', 'wsj.com', 'nytimes.com',
    'sifted.eu', 'tech.eu', 'eu-startups.com', 'finsmes.com',
    'semafor.com', 'theregister.com', 'calcalistech.com', 'globes.co.il',
    'technode.com', 'nikkei.com',
    # deal databases: compiled from announcements, so never the announcement
    'crunchbase.com', 'pitchbook.com', 'dealroom.co', 'tracxn.com',
    'cbinsights.com', 'owler.com', 'takeoffradar.com', 'stocktitan.net',
    # roundup mills and niche trade blogs — added 2026-08-14 after a live run
    # sourced most of its fundraising table to techstartups.com "venture capital
    # funding roundup" pages, which are the exact article type the funding brief
    # tells the search to exclude
    'techstartups.com', 'theaiinsider.tech', 'hpcwire.com', 'aiwire.net',
    'fitt.co', 'insider.fitt.co', 'siliconrepublic.com', 'techfundingnews.com',
    'startupdaily.net', 'businessofbusiness.com',
    # syndicators that republish someone else's wire copy under their own domain
    'morningstar.com', 'finance.yahoo.com', 'marketscreener.com',
    'streetinsider.com', 'benzinga.com', 'investing.com', 'msn.com',
    # newsletters / link blogs / community aggregators
    'tldr.tech', 'news.ycombinator.com', 'reddit.com', 'medium.com',
    'substack.com', 'analyticsindiamag.com', 'marktechpost.com',
    'the-decoder.com', 'aibusiness.com', 'artificialintelligence-news.com',
    'cryptobriefing.com', 'coindesk.com', 'cointelegraph.com',
    # Chinese aggregators
    '36kr.com', 'jiqizhixin.com', 'qbitai.com', 'ithome.com', 'sina.com.cn',
)

# Press-release wires are NOT aggregators. They carry the company's own release
# verbatim — the company pays to publish there, and the text is the company's.
# A syndicator's copy of that same release (morningstar.com/news/business-wire/…)
# is a copy, and is on the list above.
PRESS_WIRE_HOSTS = (
    'businesswire.com', 'prnewswire.com', 'globenewswire.com',
    'einpresswire.com', 'newswire.com', 'accesswire.com', 'prweb.com',
    'presseportal.de', 'kyodonews.jp',
)

# A roundup is a list of many rounds. Citing one as the source for a specific
# round is what the funding brief already forbids as subject matter, and the
# live run did it anyway on eight of seventeen rows. Matched on the path, so it
# catches a roundup wherever it is hosted.
_ROUNDUP_PATH_RE = re.compile(
    r'(?:funding|venture|vc|deal|startup)[-_]?(?:round[-_]?up|roundup|digest|'
    r'wrap|recap|weekly|daily|briefing|newsletter)'
    r'|round[-_]?up|deals?[-_]of[-_]the[-_](?:week|day|month)'
    r'|(?:week|month)[-_]in[-_](?:review|funding|venture)'
    r'|all[-_]deals|first[-_]look', re.I)


def is_roundup_url(url: str) -> bool:
    """True for a multi-company deal list rather than an article about one round."""
    if not url:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return bool(_ROUNDUP_PATH_RE.search((parts.path or '') + '?' + (parts.query or '')))


def is_press_wire(url: str) -> bool:
    """True for a wire carrying the company's own press release verbatim."""
    host = _host(url)
    if not host:
        return False
    host = host[4:] if host.startswith('www.') else host
    return any(host == h or host.endswith('.' + h) for h in PRESS_WIRE_HOSTS)

# Path fragments that mark a URL as an official announcement even on a host we
# don't recognise — a company blog lives on the company's own domain, and that
# domain is different for every company, so the shape of the path is the only
# general signal available.
_PRIMARY_PATH_HINTS = (
    '/blog', '/news', '/newsroom', '/press', '/press-release', '/pressroom',
    '/announcement', '/announcing', '/updates', '/research', '/posts',
)


def is_aggregator(url: str) -> bool:
    """
    True for a source that reports on, republishes, or compiles someone else's
    announcement rather than being it.

    A roundup counts wherever it is hosted: a "venture funding roundup August
    12" page is a list of other people's news even on a domain we don't know.
    A press wire does not count — see PRESS_WIRE_HOSTS.
    """
    if not url:
        return False
    if is_roundup_url(url) and not is_press_wire(url):
        return True
    host = _host(url)
    if not host:
        return False
    host = host[4:] if host.startswith('www.') else host
    return any(host == h or host.endswith('.' + h) for h in AGGREGATOR_HOSTS)


def is_primary_source(url: str) -> bool:
    """
    True when `url` is the announcement itself rather than coverage of it: the
    company's or investor's own domain, or a press wire carrying their release.

    Deliberately loose about *which* company domain — there is one per company
    and no list can hold them. What it is strict about is rejecting the things
    we know are downstream: trade press, syndicators, databases and roundups.
    """
    if not url or is_x_url(url):
        return False
    if is_press_wire(url):
        return True
    return not is_aggregator(url)


def _domain_name(url: str) -> str:
    """The registrable domain's name part, letters and digits only.

    'https://www.coderabbit.ai/blog/x' → 'coderabbit'
    'https://blog.discoveredmaterials.com/' → 'discoveredmaterials'
    """
    host = _host(url)
    if not host:
        return ''
    host = host.lower()
    for prefix in ('www.', 'blog.', 'news.', 'press.', 'media.', 'about.', 'ir.'):
        if host.startswith(prefix):
            host = host[len(prefix):]
            break
    parts = host.split('.')
    if len(parts) < 2:
        return re.sub(r'[^a-z0-9]', '', host)
    # Handle two-part public suffixes (.co.uk, .com.au) by stepping back one more.
    name = parts[-2]
    if name in ('co', 'com', 'org', 'net', 'gov', 'ac') and len(parts) >= 3:
        name = parts[-3]
    return re.sub(r'[^a-z0-9]', '', name)


def _name_key(name: str) -> str:
    """A company or investor name reduced to comparable letters."""
    name = re.sub(r'\b(inc|llc|ltd|limited|corp|corporation|gmbh|sa|ag|bv|plc|'
                  r'co|company|labs?|technologies|technology|holdings|group|'
                  r'ventures?|capital|partners?|management)\b', ' ', name, flags=re.I)
    return re.sub(r'[^a-z0-9]', '', name.lower())


def is_own_domain(url: str, name: str) -> bool:
    """True when `url` lives on the domain belonging to `name`."""
    domain = _domain_name(url)
    key = _name_key(name)
    if not domain or not key or len(key) < 3:
        return False
    return domain == key or key in domain or (len(domain) >= 4 and domain in key)


# Platforms where a company publishes under its own account. The domain belongs
# to the platform, so ownership is proven by the path instead:
# huggingface.co/blog/CohereLabs/… is Cohere's own model announcement, and
# treating it as third-party coverage because the domain says "huggingface" was
# wrong — it is exactly the primary source an AI report should link to.
PLATFORM_HOSTS = ('huggingface.co', 'github.com', 'gitlab.com',
                  'modelscope.cn', 'kaggle.com', 'replicate.com')


def is_platform_owned(url: str, name: str) -> bool:
    """True for `url` on a publishing platform under `name`'s own account."""
    host = _host(url)
    if not host:
        return False
    host = host[4:] if host.startswith('www.') else host
    if not any(host == h or host.endswith('.' + h) for h in PLATFORM_HOSTS):
        return False
    key = _name_key(name)
    if not key or len(key) < 3:
        return False
    segments = [re.sub(r'[^a-z0-9]', '', s.lower())
                for s in (urlsplit(url).path or '').split('/') if s]
    return any(s and (s == key or key in s or (len(s) >= 4 and s in key))
               for s in segments)


def is_own_announcement(url: str, company: str = '', investors: str = '') -> bool:
    """
    True only with positive proof that `url` is the round's own announcement:
    the company's domain, a named investor's domain, or a press wire carrying
    their release.

    This is deliberately stricter than is_primary_source(), which only knows how
    to reject sites on a denylist. A denylist cannot promise "not an aggregator"
    — the live run on 2026-08-14 proved it, slipping `pulse2.com` through as
    "primary" purely because nobody had heard of it. When the caller knows the
    company name — which the fundraising table always does — the check can
    demand evidence instead of absence of evidence.
    """
    if not url or is_x_url(url):
        return False
    if is_roundup_url(url) and not is_press_wire(url):
        return False
    if is_press_wire(url):
        return True
    if company and (is_own_domain(url, company) or is_platform_owned(url, company)):
        return True
    for investor in re.split(r'[,;、/]| and ', investors or ''):
        investor = re.sub(r'\(.*?\)', '', investor).strip()
        if len(investor) > 3 and (is_own_domain(url, investor)
                                  or is_platform_owned(url, investor)):
            return True
    return False


# Handles that post on behalf of a company but are not the company's own name,
# so the domain check needs the company, not the handle.
_HANDLE_SUFFIX_RE = re.compile(r'(?:_?ai|_?hq|_?inc|_?labs?|_?team|_?eng|_?dev)$', re.I)


def article_subject(article: dict) -> str:
    """
    Best available name for whoever the article is about, for source checking.

    The X handle is the strongest signal in this pipeline: most posts in the
    news section are companies announcing their own news, so @perplexity_ai
    linking to perplexity.ai is the common case and is exactly what we want to
    recognise as primary. Falls back to the resolved publisher name.
    """
    for key in ('subject_company', 'company'):
        if (article.get(key) or '').strip():
            return article[key].strip()

    handle = (article.get('author') or '').strip().lstrip('@')
    if not handle:
        match = _TWEET_HANDLE_PREFIX_RE.match(article.get('title') or '')
        if match:
            handle = match.group(0).strip().lstrip('@').rstrip(':：').strip()
    if handle:
        return _HANDLE_SUFFIX_RE.sub('', handle) or handle
    return (article.get('source_publisher') or '').strip()


def has_primary_source(article: dict) -> bool:
    """
    True when the article's link is the subject's own announcement.

    Same standard the fundraising table uses, applied to the news section: the
    report links to the thing that happened, not to a writeup of it.
    """
    url = news_link(article)
    if not url:
        return False
    return is_own_announcement(url, article_subject(article))


def _source_candidates(article: dict) -> list:
    """Every non-X URL this article offers, in the order it recorded them."""
    raw = []
    for key in ('source_url', 'primary_url', 'url'):
        raw.append(article.get(key) or '')
    extra = article.get('sources') or []
    if isinstance(extra, str):
        extra = [extra]
    raw.extend(extra)

    out, seen = [], set()
    for candidate in raw:
        candidate = (candidate or '').strip()
        if not candidate or is_x_url(candidate):
            continue
        key = canonical_url(candidate)
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def best_source_link(article: dict) -> str:
    """
    The URL a report should link to, primary source first.

    Ranking: an official announcement on a blog/news/press path, then any other
    non-aggregator domain, then an aggregator, then ''. An x.com link is never
    returned — it is provenance, not a source.
    """
    candidates = _source_candidates(article)
    if not candidates:
        return ''

    def rank(url: str) -> int:
        if not is_primary_source(url):
            return 2                                    # aggregator writeup
        path = (urlsplit(url).path or '').lower()
        if any(hint in path for hint in _PRIMARY_PATH_HINTS):
            return 0                                    # official announcement
        return 1                                        # company domain, other page

    # min() over a stable list keeps the recorded order as the tiebreak, so an
    # article whose links are all the same rank links where it always did.
    return min(candidates, key=rank)


# ── Headlines ─────────────────────────────────────────────────────────────────

# A headline collected from X arrives as the tweet: an @handle, hashtags, a
# thread marker, and an ellipsis where the post ran out of characters. None of
# that belongs in a news digest, and stripping it is the difference between a
# report that reads as edited and one that reads as a screenshot of a timeline.
_TWEET_HANDLE_PREFIX_RE = re.compile(r'^\s*(?:RT\s+)?@[A-Za-z0-9_]{1,15}\s*[:：]\s*')
_HANDLE_INLINE_RE = re.compile(r'(?<![A-Za-z0-9_/])@([A-Za-z0-9_]{2,15})\b')
_HASHTAG_RE = re.compile(r'(?<![A-Za-z0-9_])#[^\s#，。,.]{1,40}')
_THREAD_MARKER_RE = re.compile(
    r'(?:🧵|👇|⬇️?|\bthread\b|\ba\s+thread\b)\s*:?', re.I)
# "1/", "1/7", "(2/9)" — thread pagination, at either end of the headline.
_THREAD_INDEX_RE = re.compile(
    r'(?:^\s*\(?\d{1,2}\s*/\s*\d{0,2}\)?\s*[:.\-–—]?\s*)'
    r'|(?:\s*\(?\d{1,2}\s*/\s*\d{0,2}\)?\s*$)')
# Truncation the collector left behind: "…", "...", and the "$..." that a
# clipped dollar figure decays into.
_TRUNCATION_RE = re.compile(r'\s*(?:[$＄]\s*)?(?:…+|\.{3,}|。{2,})\s*$')
_EMOJI_RE = re.compile(
    '[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF️‍]+')


def clean_headline(title: str) -> str:
    """
    Turn a collected headline into one a news digest can print.

    Removes the @handle the post was authored by, inline @mentions, hashtags,
    thread markers ("🧵", "1/7", "👇"), decorative emoji, and the trailing
    ellipsis left by X's character limit. An inline @mention becomes the bare
    name — "@perplexity_ai" is Perplexity, and dropping it entirely would lose
    the subject of the sentence.

    Falls back to the original string if cleaning would empty it: a headline
    that is nothing but a handle and an emoji is still better than a blank cell.
    """
    if not title:
        return ''

    text = _TWEET_HANDLE_PREFIX_RE.sub('', title)
    text = _THREAD_INDEX_RE.sub('', text)
    text = _THREAD_MARKER_RE.sub(' ', text)
    text = _HASHTAG_RE.sub(' ', text)
    text = _HANDLE_INLINE_RE.sub(r'\1', text)
    text = _EMOJI_RE.sub(' ', text)
    text = _TRUNCATION_RE.sub('', text)

    # Collapse the whitespace the substitutions above left behind, and tidy the
    # punctuation that ends up stranded at the edges.
    text = re.sub(r'\s+', ' ', text).strip()
    text = text.strip('"“”‘’ ')
    text = re.sub(r'^[\-–—:：,，.。]+\s*', '', text)
    text = re.sub(r'\s*[,，:：\-–—]+$', '', text).strip()

    return text or title.strip()


# ── Funding stories ───────────────────────────────────────────────────────────

# A funding round is reported in the fundraising table, with its amount, stage,
# valuation and investors in named columns. The same round retold as a prose
# summary in the news section is the identical fact, twice, in a worse format —
# so the news section drops it.
_FUNDING_EVENT_RE = _phrase_re([
    # Round names. These are unambiguous on their own.
    'funding round', 'seed round', 'pre-seed', 'preseed', 'seed funding',
    'series a', 'series b', 'series c', 'series d', 'series e', 'series f',
    'growth round', 'bridge round', 'venture round',
    # The round as an event.
    'led the round', 'co-led by', 'oversubscribed', 'raised an undisclosed',
    'raises an undisclosed', 'closes funding', 'closed funding',
    'secures funding', 'secured funding', 'raises funding',
    'funding at a valuation', 'post-money', 'pre-money',
    'at a valuation of', 'now valued at',
    'emerges from stealth with', 'out of stealth with',
    # Bare 'raised' is deliberately absent — "MoK raised throughput 40%" is a
    # kernel benchmark, and it was moving model releases into the funding
    # bucket. A raise without a round name still matches _FUNDING_MONEY_RE
    # below, which requires the verb and a figure together.
])
_FUNDING_EVENT_ZH_RE = re.compile(
    '|'.join(re.escape(k) for k in (
        # 估值 and 参投 are deliberately absent: a VC describing its accelerator's
        # terms uses both, and that is a programme, not a round.
        '融资', '轮融资', '领投', '跟投', '天使轮', '种子轮', 'Pre-A轮',
        '战略投资', '超额认购',
    )))
# "raised $50M", "closes $12 million" — the verb and the figure together, which
# catches phrasings the vocabulary above spells differently.
_FUNDING_MONEY_RE = re.compile(
    r'\b(?:rais\w+|secur\w+|clos\w+|land\w+|bag\w+|nab\w+|net\w+|pull\w+ in|'
    r'walk\w+ away with|announc\w+)\b[^.。\n]{0,40}?'
    r'[$€£]\s?\d[\d,.]*\s?(?:k|m|b|bn|mm|million|billion)?', re.I)


def is_funding_story(article: dict) -> bool:
    """
    True when the article's subject is a funding round, acquisition or IPO.

    Checked against the headline and summary rather than the full body: an
    article about a product launch routinely mentions that the company "raised
    $30M last year", and matching on the body would move that launch out of the
    news section over a background clause.
    """
    if (article.get('content_type') or '').lower() in ('funding', 'fundraising'):
        return True

    text = ' '.join([
        article.get('title') or '',
        article.get('source_title') or '',
        article.get('summary') or article.get('description') or '',
    ])
    if not text.strip():
        return False

    if _FUNDING_EVENT_RE.search(text) or _FUNDING_EVENT_ZH_RE.search(text):
        return True
    return bool(_FUNDING_MONEY_RE.search(text))


# ── News section scope ────────────────────────────────────────────────────────

# The summarizer sorts every article into one of these after reading the whole
# thing (SUMMARY_PROMPT_AI in summarize_articles.py). These are the news the AI
# News Summary exists to carry.
SUBSTANTIVE_CATEGORIES = {
    '模型与研究',    # model releases, research, benchmarks
    '产品与应用',    # product launches and features
    '政策与安全',    # policy, regulation, safety
    '行业动态',      # partnerships, strategy, market moves
    '大科技公司',    # big tech — only reaches here with --include-frontier
}

# NON_AI_CATEGORY ('非AI') is the summarizer's explicit not-about-AI verdict, and
# curate() already drops it. '其他' is "AI-adjacent but none of the above" — the
# filler bucket, and the one the report is meant to cut.
TANGENTIAL_CATEGORIES = {NON_AI_CATEGORY, '其他'}


def is_substantively_ai(article: dict) -> bool:
    """
    True when the story is actually about AI — a model or product release,
    research result, partnership, or policy development — rather than something
    AI-adjacent that filled a slot.

    The judgement is the summarizer's `category`, not a keyword match, because
    the summarizer read the whole article and a keyword match reads a headline.
    That distinction is not academic: "Today we're launching Projects, an
    evolution of Spaces" is a Perplexity product launch with no AI term in it,
    and a keyword gate drops it while keeping any funding post that says "AI" in
    passing.

    Articles with no category — added by hand, or summarized before the field
    existed — fall back to the keyword test rather than being trusted blindly.
    """
    category = (article.get('category') or '').strip()
    if category in TANGENTIAL_CATEGORIES:
        return False
    if category in SUBSTANTIVE_CATEGORIES:
        return True

    text = ' '.join([
        article.get('title') or '',
        article.get('source_title') or '',
        article.get('summary') or article.get('description') or '',
        article.get('content') or '',
    ])
    return is_ai_relevant(text)


def signal_score(article: dict, default: int = 3) -> int:
    """The summarizer's 1-5 relevance score, as an int.

    Stored as a string by summarize_articles.py and absent entirely on articles
    from older runs, so both are normalized here rather than at each call site.
    """
    try:
        return int(str(article.get('relevance', default)).strip()[:1])
    except (ValueError, IndexError):
        return default


def filter_news_section(articles: list, no_funding: bool = False,
                        min_signal: int = 3,
                        require_primary_source: bool = True) -> tuple:
    """
    Reduce the curated articles to what the AI News Summary should carry.

    Three rules, applied in this order so the reported reason is the one a
    reader would give:

      1. **No funding rounds.** They are the fundraising table's subject, with
         better columns and wider sourcing. Skipped when `no_funding` is set and
         there is no table to move them to.
      2. **Substantively about AI.** A launch, a model, a research result, a
         partnership, a policy development — not a company with "AI" in its
         boilerplate.
      3. **Above the signal floor.** The summarizer's 1-5 relevance score; 3 and
         up by default, which is what cuts filler.
      4. **Linked to a primary source.** The subject's own announcement, not a
         writeup of it. Run `upgrade_to_primary_sources()` (resolve_sources.py)
         before this, which goes and finds the announcement for the ones that
         arrived pointing at an aggregator; what this drops is the residue that
         nobody announced publicly. Set `require_primary_source=False` to keep
         aggregator-linked items instead.

    Returns (kept, removed_by_reason). Both AI report generators call this, so
    the two layouts stay one editorial line rather than two that drift.
    """
    kept, removed = [], {'funding': [], 'not AI': [], 'low signal': [],
                         'aggregator source': []}

    for article in articles:
        if not no_funding and is_funding_story(article):
            removed['funding'].append(article)
        elif not is_substantively_ai(article):
            removed['not AI'].append(article)
        elif signal_score(article) < min_signal:
            removed['low signal'].append(article)
        elif require_primary_source and not has_primary_source(article):
            removed['aggregator source'].append(article)
        else:
            kept.append(article)

    detail = ', '.join(f"{name} ({len(items)})"
                       for name, items in removed.items() if items)
    if detail:
        print(f"  News section scope: removed {detail}")
    print(f"  News section: {len(kept)} article(s)")
    return kept, removed


# ── Deduplication ─────────────────────────────────────────────────────────────

STOP_WORDS = {
    'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
    'of', 'with', 'by', 'from', 'is', 'are', 'was', 'were', 'be', 'been',
    'has', 'have', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
    'should', 'may', 'might', 'this', 'that', 'these', 'those', 'it', 'its',
    'as', 'up', 'out', 'about', 'into', 'through', 'new', 'can', 'how',
    'what', 'says', 'said', 'after', 'over', 'now', 'just', 'also', 'we',
    'our', 'you', 'your', 'they', 'their',
}

_HANDLE_PREFIX_RE = re.compile(r'^@[A-Za-z0-9_]+:\s*')


def _significant_words(text: str) -> set:
    text = _HANDLE_PREFIX_RE.sub('', text or '').lower()
    text = _URL_RE.sub(' ', text)
    text = re.sub(r'[^\w\s]', ' ', text)
    words = {w for w in text.split() if len(w) > 2 and w not in STOP_WORDS}
    # CJK text has no spaces, so word splitting yields one long token; fall back
    # to character bigrams so Chinese articles compare meaningfully too.
    cjk = re.findall(r'[一-鿿]{2,}', text)
    for run in cjk:
        words.update(run[i:i + 2] for i in range(len(run) - 1))
    return words


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# Words that start a sentence in an announcement and get capitalized for that
# reason alone. Stripped from the front of a candidate name phrase, because
# "Introducing Cursor" is a phrase about Cursor, not a name in itself.
_PHRASE_LEAD_STOPWORDS = {
    'a', 'an', 'the', 'this', 'that', 'these', 'those', 'our', 'we', 'my',
    'i', 'it', 'they', 'you', 'your', 'his', 'her', 'their',
    'introducing', 'announcing', 'presenting', 'launching', 'today',
    'tomorrow', 'yesterday', 'now', 'new', 'read', 'watch', 'listen', 'learn',
    'get', 'try', 'meet', 'see', 'join', 'check', 'big', 'happy', 'welcome',
    'congrats', 'congratulations', 'excited', 'proud', 'thrilled', 'first',
    'more', 'also', 'just', 'why', 'how', 'what', 'when', 'here', 'there',
    'in', 'on', 'at', 'to', 'for', 'with', 'from', 'and', 'but', 'so',
}

# A run of two or more capitalized/alphanumeric tokens: product and company
# names as they appear in announcements — "Nano Banana 2 Lite", "Aseon Labs",
# "Crane Venture Partners", "AI SDK 7".
#
# A word token may not contain '.', so a phrase cannot run across a sentence
# boundary: "…led by Greenoaks. Sierra raised…" was yielding the name
# "greenoaks. sierra", which is not a name, and which cost the real evidence
# ("Sierra") its own marker. Version numbers still work — "Opus 4.8" matches
# through the numeric alternative.
_NAME_PHRASE_RE = re.compile(
    r'(?<![A-Za-z0-9_])(?:[A-Z][A-Za-z0-9\'’-]*|\d+(?:\.\d+)?)'
    r'(?:\s+(?:[A-Z][A-Za-z0-9\'’-]*|\d+(?:\.\d+)?)){1,5}')


def name_phrases(text: str) -> set:
    """
    Distinctive multi-word names in `text`, lowercased.

    These are the dedup signal that word overlap can't provide. Measured on real
    collected posts, the same event written up by two different accounts lands
    at 0.27-0.45 word overlap — below the primary threshold, and indistinguishable
    from unrelated posts, which sit in the same band. What separates them is that
    the real duplicates share a *name*: "Nano Banana 2 Lite" appears verbatim in
    both write-ups of that launch, while two unrelated "Learn more" posts share
    only common words.

    Leading sentence-starter capitals are stripped, so "Introducing Computer for
    Counsel" yields "computer for counsel" rather than a phrase that would match
    every other post beginning "Introducing".
    """
    if not text:
        return set()
    phrases = set()
    for match in _NAME_PHRASE_RE.finditer(_URL_RE.sub(' ', text)):
        tokens = match.group(0).split()
        while tokens and tokens[0].lower().strip('.,\'’-') in _PHRASE_LEAD_STOPWORDS:
            tokens.pop(0)
        while tokens and tokens[-1].lower().strip('.,\'’-') in _PHRASE_LEAD_STOPWORDS:
            tokens.pop()
        if len(tokens) >= 2:
            phrases.add(' '.join(tokens).lower().strip('.,'))
    return phrases


# Names and figures too common in AI startup news to identify a story. Without
# this list, "Sierra raised $350M Series C led by Kleiner" and "Harvey raised
# $200M Series C led by Sequoia" share "Series C" and enough funding boilerplate
# to look like one story — two different rounds collapsed into one entry.
GENERIC_MARKERS = {
    'series a', 'series b', 'series c', 'series d', 'series e', 'series f',
    'seed round', 'pre-seed', 'series a funding', 'y combinator', 'yc',
    'ai', 'llm', 'llms', 'api', 'apis', 'saas', 'gpu', 'gpus', 'sdk',
    'ceo', 'cto', 'coo', 'cfo', 'vp', 'founder', 'co-founder',
    'techcrunch', 'venturebeat', 'bloomberg', 'reuters', 'forbes', 'wired',
    'the information', 'the verge', 'business insider', 'axios', 'cnbc',
    'san francisco', 'new york', 'silicon valley', 'united states', 'us', 'usa',
    'uk', 'eu', 'europe', 'china', 'india', 'japan',
    'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday',
    'q1', 'q2', 'q3', 'q4',
}

# A capitalized single word can identify a company (Sierra, Cursor, Harvey), so
# it counts as a marker — but only these ones, which are neither generic nor
# ordinary sentence-starters.
#
# Explicit boundaries rather than \b: CJK characters are word characters to
# Python's regex, so \b never matches between 中文 and a Latin capital. In
# "加拿大AI公司Cohere与滑铁卢大学" — ordinary phrasing in a Chinese summary — the
# company name yielded no marker at all.
_PROPER_TOKEN_RE = re.compile(
    r'(?<![A-Za-z0-9_])[A-Z][A-Za-z0-9]{2,}(?![A-Za-z0-9_])')

# Word overlap required alongside shared markers, to call two items the same
# story. Lower than the primary threshold because the shared names are doing
# the work; not zero, because one company can have two unrelated stories in a
# fortnight and those must both survive.
NAME_MATCH_MIN_OVERLAP = 0.25

# How much shared evidence makes two items the same story. Both conditions
# hold, and both were derived from what actually goes wrong on real posts:
#
#   ≥1 strong marker — a multi-word name or a figure. Two items sharing only
#     bare words ("Runway", "agent") are two different launches on the same
#     platform, which is a story each, not one story twice.
#   ≥2 markers total — one shared name is routinely a coincidence: an
#     availability announcement and a quote-tweet reacting to it share exactly
#     one product name and 0.31 word overlap.
#
# Every true duplicate in the sample cleared both bars; every false one failed
# at least one.
MIN_SHARED_MARKERS = 2
MIN_SHARED_STRONG_MARKERS = 1

# Abundant shared evidence earns a lower word-overlap bar. "Sierra raised $350
# million led by Greenoaks" and "Sierra lands a $350M round at a $10B valuation,
# Greenoaks leading" share the company, the investor and the amount, and only
# 0.167 of their words — two wordings of one round, which is precisely the
# duplicate a funding report must not print twice. Three shared markers is
# specific enough that the wording no longer has to agree.
STRONG_EVIDENCE_MIN_MARKERS = 3
# 0.10, not 0.12: measured across all 561 pairs of a real report, summary
# overlap between unrelated stories has median 0.031 and p90 0.058, so this
# floor sits far above the noise. Between 0.08 and 0.12 the outcome on that
# report is identical — the three-marker gate is what filters, and this is a
# sanity check on top of it, not the discriminator.
STRONG_EVIDENCE_MIN_OVERLAP = 0.10


def _strong(markers: set) -> set:
    """Markers specific enough to identify a story on their own terms."""
    return {m for m in markers if m[0] in 'nm'}


def story_markers(text: str) -> set:
    """
    The identifying details of a story: names, and the figures in it.

    Returns a set of type-tagged markers — 'n:aseon labs' (name phrase),
    't:sierra' (proper noun), 'm:350000000.0' (money or percentage, normalized
    so "$350M" and "350 million dollars" are one marker). Type tags keep a
    company called "Series" from matching a funding stage.

    Two stories that share two of these are the same story told twice; see
    MIN_SHARED_MARKERS for why one is not enough.
    """
    if not text:
        return set()
    clean = _URL_RE.sub(' ', text)

    phrases = {p for p in name_phrases(text) if p not in GENERIC_MARKERS}
    markers = {f'n:{p}' for p in phrases}
    phrase_words = {w for p in phrases for w in p.split()}
    for token in _PROPER_TOKEN_RE.findall(clean):
        low = token.lower()
        if low in GENERIC_MARKERS or low in STOP_WORDS or low in _PHRASE_LEAD_STOPWORDS:
            continue
        # Skip a word already inside a name phrase. It is the same evidence
        # counted twice, and counting it twice is what let "Opus 4.8" alone
        # satisfy a two-marker rule.
        if low in phrase_words:
            continue
        markers.add(f't:{low}')
    for value, is_percent, has_currency, _raw in _iter_figures(clean):
        if _is_checkable(value, is_percent, has_currency):
            markers.add(f'm:{value}')
    return markers


def _richness(article: dict) -> tuple:
    """
    Sort key for picking the survivor among duplicates: an item that links to
    real coverage beats one that only links to X, then longer content wins.
    """
    has_link = 1 if news_link(article) else 0
    body = len(article.get('content') or '') + len(article.get('description') or '')
    summarized = 1 if article.get('summary') else 0
    return (has_link, summarized, body)


def dedupe(articles: list, threshold: float = 0.55) -> tuple:
    """
    Collapse duplicate stories. Returns (unique, stats).

    Three ways the same news shows up twice, all handled here:

      same-url    two posts linking to the same article (after canonicalization,
                  so utm parameters don't hide it) — including the case where
                  one is the x.com post and the other is coverage of it.
      same-text   a thread head and its standalone repost, or the same wording
                  quoted by two accounts.
      near-text   two accounts writing up the same event in their own words —
                  caught by word-set overlap above `threshold`.
      same-story  the launch as the company announced it and as the press wrote
                  it up: too differently worded for the overlap test, but naming
                  the same products, companies and figures. Requires two shared
                  markers AND a lower bar of word overlap — see story_markers().

    The richest version survives (see _richness), so collapsing a duplicate
    never costs the report its only link to the story.
    """
    stats = {'same-url': 0, 'same-text': 0, 'near-text': 0, 'same-story': 0}
    kept = []            # [(article, words, canon, names, summary_words)]
    by_url = {}          # canonical url -> index in kept
    by_text = {}         # exact normalized text -> index in kept

    def _replace(idx, article, words, canon, names, summary_words):
        old = kept[idx][0]
        # Keep the union of both copies' names either way: the surviving article
        # should match anything either of them would have matched, or a third
        # write-up could slip through by resembling only the discarded one.
        merged_names = names | kept[idx][3]
        if _richness(article) > _richness(old):
            old_canon = kept[idx][2]
            kept[idx] = (article, words, canon or old_canon, merged_names,
                         summary_words or kept[idx][4])
            if old_canon and by_url.get(old_canon) == idx and canon and canon != old_canon:
                by_url[canon] = idx
        else:
            kept[idx] = (kept[idx][0], kept[idx][1], kept[idx][2], merged_names,
                         kept[idx][4] or summary_words)

    for article in articles:
        canon = canonical_url(news_link(article) or article.get('url', ''))
        body = (article.get('title', '') + ' ') + article_text(article)[:400]
        words = _significant_words(body)
        # Markers also come from the summary when one exists. A summary is a
        # normalized account of the event, so two write-ups converge there even
        # when their source wording does not: on a real run, two reports of the
        # same pre-seed round shared only the company name in their source text
        # — the founder's name and the amount appeared in both summaries.
        # Word overlap stays on the source text, where it is not diluted by
        # summary boilerplate.
        summary = article.get('summary') or ''
        names = story_markers(body + ' ' + summary)
        # Overlap is scored on the source text and, when it exists, the summary
        # — whichever agrees more. Two accounts of one event can share almost no
        # source wording (a company's own post versus the other party's) and
        # still describe the same thing once summarized.
        summary_words = _significant_words(summary) if summary else None
        exact = ''.join(sorted(words))[:200]

        if canon and canon in by_url:
            stats['same-url'] += 1
            _replace(by_url[canon], article, words, canon, names, summary_words)
            continue
        if exact and exact in by_text:
            stats['same-text'] += 1
            _replace(by_text[exact], article, words, canon, names, summary_words)
            continue

        dupe_idx = dupe_kind = None
        for i, (_, kept_words, _, kept_names, kept_summary) in enumerate(kept):
            overlap = _jaccard(words, kept_words)
            if overlap >= threshold:
                dupe_idx, dupe_kind = i, 'near-text'
                break
            shared = names & kept_names
            if (len(shared) >= MIN_SHARED_MARKERS
                    and len(_strong(shared)) >= MIN_SHARED_STRONG_MARKERS):
                floor = (STRONG_EVIDENCE_MIN_OVERLAP
                         if len(shared) >= STRONG_EVIDENCE_MIN_MARKERS
                         else NAME_MATCH_MIN_OVERLAP)
                agreement = overlap
                if summary_words and kept_summary:
                    agreement = max(agreement, _jaccard(summary_words, kept_summary))
                if agreement >= floor:
                    dupe_idx, dupe_kind = i, 'same-story'
                    break
        if dupe_idx is not None:
            stats[dupe_kind] += 1
            _replace(dupe_idx, article, words, canon, names, summary_words)
            continue

        kept.append((article, words, canon, names, summary_words))
        idx = len(kept) - 1
        if canon:
            by_url.setdefault(canon, idx)
        if exact:
            by_text.setdefault(exact, idx)

    return [a for a, _, _, _, _ in kept], stats


# ── Curation ──────────────────────────────────────────────────────────────────

def corroborates(post_text: str, article_text_: str, threshold: float = 0.45) -> tuple:
    """
    Does `article_text_` actually cover the event described in `post_text`?

    Returns (corroborated, score). Used to check a source URL that a web search
    proposed for a post: the model can return a plausible-looking article about
    the right company and the wrong event, and a link that doesn't back the
    summary is worse than no link at all.

    The measure is the share of the post's distinctive words that appear in the
    article — not Jaccard, because the article is 50x longer and symmetric
    overlap would always look small.
    """
    post_words = _significant_words(post_text)
    if len(post_words) < 3:
        return False, 0.0
    article_words = _significant_words(article_text_ or '')
    if not article_words:
        return False, 0.0
    score = len(post_words & article_words) / len(post_words)
    return score >= threshold, round(score, 3)


# Sources a person chose by hand rather than a collector swept up. The
# editorial filters are heuristics for triaging a firehose; applied to an
# article somebody deliberately pasted in, they are just a way to silently
# discard an explicit instruction.
HAND_PICKED_SOURCES = ('wechat', 'manual')


def is_hand_picked(article: dict) -> bool:
    """True for articles a person added deliberately (pasted WeChat URLs)."""
    if article.get('manual'):
        return True
    source = (article.get('source') or '').lower()
    return any(s in source for s in HAND_PICKED_SOURCES)


def curate(articles: list, include_frontier: bool = False,
           include_opinion: bool = False, dedupe_only: bool = False,
           require_source: bool = False) -> list:
    """
    Apply the report's editorial rules to a list of articles, printing what
    each one removed. Returns the articles that belong in the news table.

    This runs even though collect_x.py already filtered, because it is the last
    point where every article is in one place: items added by hand, WeChat
    articles, and anything collected before these rules existed all pass
    through here. It also knows things collection couldn't — `subject_type` and
    `content_type`, which the summarizer assigns after reading the full article.

    Order matters. Dedup runs first, so a company named in the "out of scope"
    tally below is one story about it, not the same story counted three times.

    `dedupe_only` keeps the deduplication and skips the editorial filters — for
    reports that are not the AI startup brief (the deeptech one, for instance,
    where "no frontier labs" is not the editorial line).

    `require_source` drops items with no link to real coverage. Run
    tools/resolve_sources.py first: it finds the article behind a link-less
    post, so what this removes afterwards is the residue no publication
    covered — which is a tweet, not news.
    """
    unique, dedup_stats = dedupe(articles)
    removed = len(articles) - len(unique)
    if removed:
        detail = ', '.join(f"{k}={v}" for k, v in dedup_stats.items() if v)
        print(f"  Duplicates removed: {removed} ({detail})")

    if dedupe_only:
        return unique

    labs = [] if include_frontier else get_labs()
    kept, dropped, hand_picked = [], {}, 0
    for article in unique:
        # A pasted WeChat URL is an instruction, not a candidate. It still went
        # through dedup above — the same article twice is still once — but it is
        # never dropped for being a statement, an opinion, or about a big
        # company, because someone asked for it by name.
        if is_hand_picked(article):
            hand_picked += 1
            kept.append(article)
            continue

        reason = ''
        if not include_frontier:
            name, why = frontier_match(article, labs)
            if name:
                reason = f'{name} — {why}'
        if not reason:
            summary_reason = excluded_by_summary(article)
            if include_frontier and summary_reason.startswith('subject is'):
                summary_reason = ''
            elif include_opinion and summary_reason in (
                    'commentary, not news', 'no event reported',
                    'a statement, not an event'):
                summary_reason = ''
            reason = summary_reason
        if not reason and require_source and not news_link(article):
            reason = 'no source article found'
        if reason:
            dropped.setdefault(reason.split(' — ')[0], []).append(article)
            continue
        kept.append(article)

    if dropped:
        total = sum(len(v) for v in dropped.values())
        ranked = sorted(dropped.items(), key=lambda kv: -len(kv[1]))
        print(f"  Out of scope: {total} article(s) — "
              + ', '.join(f"{name} ({len(items)})" for name, items in ranked))
        unlinked = len(dropped.get('no source article found', []))
        if unlinked:
            print(f"    ({unlinked} dropped for having no source article. Run "
                  f"tools/resolve_sources.py before this step to find the coverage "
                  f"behind them, or pass --allow-unlinked to keep them unlinked.)")
    scope = 'AI news' if include_frontier else 'smaller AI startups'
    detail = f" ({hand_picked} hand-picked, kept unfiltered)" if hand_picked else ''
    print(f"  News table: {len(kept)} article(s) about {scope}{detail}")
    return kept


# ── Self-tests ────────────────────────────────────────────────────────────────

def _test():
    import io, contextlib
    labs = load_labs(DEFAULT_LABS_FILE)
    assert labs, "frontier_labs.txt should parse to at least one lab"
    failures = []

    def check(name, got, want):
        if got != want:
            failures.append(f"{name}: got {got!r}, want {want!r}")

    # Frontier: the lab's own account
    check('author match',
          frontier_match({'author': '@OpenAI', 'description': 'We are introducing GeneBench.'},
                         labs)[0], 'OpenAI')
    # Frontier: link to the lab's own site
    check('domain match',
          frontier_match({'author': '@someone',
                          'description': 'Read more',
                          'source_url': 'https://www.anthropic.com/news/claude-4'},
                         labs)[0], 'Anthropic')
    # Frontier: lab opens the story
    check('subject match',
          frontier_match({'author': '@techpress',
                          'description': 'Google DeepMind released Gemini 3 today.'},
                         labs)[0], 'Google DeepMind')
    # Startup story that merely mentions a lab further in — must survive
    check('passing mention kept',
          frontier_match({'author': '@techpress',
                          'description': 'Cursor, the AI coding startup, raised $200M at a '
                                         '$2.5B valuation to compete with OpenAI Codex.'},
                         labs)[0], None)
    check('ex-lab founder kept',
          frontier_match({'author': '@vcguy',
                          'description': 'Reflection AI, founded by two former DeepMind '
                                         'researchers, launched its coding agent today.'},
                         labs)[0], None)
    # Word boundaries: no substring false positives
    check('no substring match',
          frontier_match({'author': '@dev', 'description':
                          'Metadata tooling startup Metaplane raised a seed round.'},
                         labs)[0], None)

    # Opinion vs news
    check('opinion marker', is_opinion('My take: agents are overhyped this year.')[0], True)
    check('unpopular opinion', is_opinion('Unpopular opinion: RAG is dead.')[0], True)
    check('imo boundary', is_opinion('The new tax will impose limits on compute.')[0], False)
    check('plain news', is_opinion('Harvey raised a $300M Series E led by Kleiner.')[0], False)
    check('no-signal strict',
          is_opinion('Agents are going to change everything about work.',
                     require_news_signal=True)[0], True)

    # CEO statements: a quote is not an event, unless an event is being quoted
    check('exec town hall',
          is_statement('Zuckerberg told staff that AI agents have not progressed '
                       'as quickly as he had hoped.')[0], True)
    check('exec interview',
          is_statement('Speaking at the conference, the CEO said inference costs '
                       'will keep falling.')[0], True)
    check('exec prediction',
          is_statement('The founder predicts most SaaS will be rewritten by 2028.')[0], True)
    check('event via quote kept',
          is_statement("Sierra's CEO said the company raised $350M at a $10B "
                       "valuation.")[0], False)
    check('launch via quote kept',
          is_statement('The team said it has launched its coding agent in public '
                       'beta today.')[0], False)
    check('plain event not a statement',
          is_statement('Harvey acquired a legal research startup.')[0], False)
    check('statement routed through is_opinion',
          is_opinion('The CEO told investors the market is maturing.')[0], True)
    check('chinese news kept',
          is_opinion('某公司完成A轮融资，由红杉中国领投。', require_news_signal=True)[0], False)

    # Links
    check('extract source',
          extract_source_url('We shipped it today https://blog.startup.ai/launch cool',
                             expand=False),
          'https://blog.startup.ai/launch')
    check('skip x link',
          extract_source_url('see https://x.com/foo/status/1 and https://news.site/a',
                             expand=False),
          'https://news.site/a')
    check('no link', extract_source_url('Just text, no links here.', expand=False), '')
    check('trailing punctuation',
          extract_source_url('Read (https://news.site/a).', expand=False),
          'https://news.site/a')
    check('news_link prefers source',
          news_link({'url': 'https://x.com/a/status/1',
                     'source_url': 'https://news.site/a'}), 'https://news.site/a')
    check('news_link drops x-only',
          news_link({'url': 'https://x.com/a/status/1'}), '')
    check('canonical strips tracking',
          canonical_url('https://WWW.News.site/a/?utm_source=x&id=7#top'),
          'https://news.site/a?id=7')

    # Primary sources over aggregators
    check('techcrunch is an aggregator',
          is_aggregator('https://techcrunch.com/2026/08/11/acme-raises/'), True)
    check('company blog is not',
          is_aggregator('https://acme.ai/blog/series-a'), False)
    check('prefers company blog over techcrunch',
          best_source_link({'url': 'https://techcrunch.com/2026/08/11/acme/',
                            'source_url': 'https://acme.ai/blog/launch'}),
          'https://acme.ai/blog/launch')
    check('prefers announcement path over bare company page',
          best_source_link({'source_url': 'https://acme.ai/product',
                            'sources': ['https://acme.ai/blog/launch']}),
          'https://acme.ai/blog/launch')
    check('aggregator kept when it is all there is',
          best_source_link({'source_url': 'https://techcrunch.com/2026/08/11/acme/'}),
          'https://techcrunch.com/2026/08/11/acme/')
    check('x links never returned',
          best_source_link({'url': 'https://x.com/a/status/1',
                            'sources': ['https://twitter.com/b/status/2']}), '')

    # Roundups and syndicators — every URL below is one the 2026-08-14 live
    # funding run actually shipped as a row's source.
    check('techstartups roundup is an aggregator',
          is_aggregator('https://techstartups.com/2026/08/12/venture-capital-'
                        'startup-funding-roundup-august-12-2026-coatue/'), True)
    check('axios all-deals first-look is an aggregator',
          is_aggregator('https://www.axios.com/pro/all-deals/2026/08/10/'
                        'pro-rata-premium-first-look-point2-bowman-jazz'), True)
    check('morningstar copy of a wire release is an aggregator',
          is_aggregator('https://www.morningstar.com/news/business-wire/'
                        '20260810167426/discovered-materials-closes-9m-seed-round'), True)
    check('roundup detected on an unknown host',
          is_aggregator('https://some-blog.example/2026/08/12/'
                        'weekly-funding-roundup/'), True)
    check('business wire original is primary',
          is_primary_source('https://www.businesswire.com/news/home/2026081012345/'
                            'en/Discovered-Materials-Closes-9M-Seed'), True)
    check('press wire beats the roundup path check',
          is_aggregator('https://www.prnewswire.com/news-releases/'
                        'acme-funding-roundup-item-123.html'), False)
    check('company blog is primary',
          is_primary_source('https://echovane.com/blog/our-seed-round'), True)
    check('techcrunch article is not primary',
          is_primary_source('https://techcrunch.com/2026/08/11/river-ai/'), False)
    check('roundup url detection',
          is_roundup_url('https://x.example/2026/08/13/deals-of-the-week/'), True)
    check('ordinary article is not a roundup',
          is_roundup_url('https://x.example/2026/08/13/acme-raises-5m/'), False)
    # is_own_announcement: positive proof, not absence from a denylist
    check('company domain proves ownership',
          is_own_announcement('https://coderabbit.ai/blog/series-c', 'CodeRabbit'), True)
    check('multiword company domain',
          is_own_announcement('https://discoveredmaterials.com/news/seed',
                              'Discovered Materials'), True)
    check('company blog subdomain',
          is_own_announcement('https://blog.echovane.com/seed', 'Echovane'), True)
    check('suffix ignored when matching',
          is_own_announcement('https://vitalis.ai/press/a', 'Vitalis AI Inc.'), True)
    check('investor domain proves ownership',
          is_own_announcement('https://www.generalcatalyst.com/stories/river-ai',
                              'River AI', 'General Catalyst (lead), Index'), True)
    check('unknown trade blog is NOT proof',
          is_own_announcement('https://pulse2.com/flagler-health-raises-50-million/',
                              'Flagler Health'), False)
    check('techcrunch is NOT proof',
          is_own_announcement('https://techcrunch.com/2026/08/11/river-ai/',
                              'River AI'), False)
    check('press wire is proof',
          is_own_announcement('https://www.businesswire.com/news/home/1/en/Acme',
                              'Acme'), True)
    check('roundup on a company domain is still rejected',
          is_own_announcement('https://acme.com/blog/weekly-funding-roundup/',
                              'Acme'), False)
    check('domain name extraction',
          [_domain_name('https://www.coderabbit.ai/blog/x'),
           _domain_name('https://blog.acme.co.uk/p')], ['coderabbit', 'acme'])

    # Company-owned pages on a publishing platform
    check('huggingface org blog is the org announcing',
          is_own_announcement('https://huggingface.co/blog/CohereLabs/meet-north',
                              'Cohere'), True)
    check('huggingface page of a different org is not',
          is_own_announcement('https://huggingface.co/blog/meta/llama',
                              'Cohere'), False)

    # News-section source checking
    check('company posting its own blog is primary',
          has_primary_source({'author': '@perplexity_ai',
                              'source_url': 'https://www.perplexity.ai/hub/blog/projects'}),
          True)
    check('company posting an aggregator writeup is not',
          has_primary_source({'author': '@cursor_ai',
                              'source_url': 'https://www.marktechpost.com/2026/08/04/cursor-mok/'}),
          False)
    check('resolved subject beats the handle',
          has_primary_source({'author': '@VentureBeat', 'subject_company': 'Skan AI',
                              'source_url': 'https://www.skan.ai/news/series-b'}),
          True)
    check('unlinked article has no primary source',
          has_primary_source({'author': '@acme'}), False)
    check('subject from handle strips the ai suffix',
          article_subject({'author': '@cursor_ai'}), 'cursor')
    check('aggregator-sourced items are dropped',
          len(filter_news_section(
              [{'author': '@cursor_ai', 'category': '模型与研究', 'relevance': 4,
                'title': 'Cursor open-sources MoK',
                'source_url': 'https://www.marktechpost.com/2026/08/04/cursor-mok/'}],
              )[0]), 0)
    check('aggregator-sourced items kept with the escape hatch',
          len(filter_news_section(
              [{'author': '@cursor_ai', 'category': '模型与研究', 'relevance': 4,
                'title': 'Cursor open-sources MoK',
                'source_url': 'https://www.marktechpost.com/2026/08/04/cursor-mok/'}],
              require_primary_source=False)[0]), 1)

    check('primary beats roundup in ranking',
          best_source_link({'sources': [
              'https://techstartups.com/2026/08/12/venture-capital-funding-roundup/',
              'https://coderabbit.ai/blog/series-c']}),
          'https://coderabbit.ai/blog/series-c')

    # Headlines
    check('strips handle prefix',
          clean_headline("@perplexity_ai: Today we're launching Projects…"),
          "Today we're launching Projects")
    check('strips RT prefix and hashtag',
          clean_headline('RT @acme: Acme ships v2 #AI #startups'), 'Acme ships v2')
    check('strips thread markers',
          clean_headline('1/7 Acme raises a Series A 🧵👇'), 'Acme raises a Series A')
    check('keeps inline mention as a name',
          clean_headline('@acme partners with @stripe on payments'),
          'acme partners with stripe on payments')
    check('strips clipped dollar figure',
          clean_headline('Simile closes a round of $...'), 'Simile closes a round of')
    check('clean headline left alone',
          clean_headline('Mistral releases Large 3'), 'Mistral releases Large 3')
    check('never returns empty', clean_headline('🧵👇'), '🧵👇')

    # Funding stories belong in the fundraising table, not the news section
    check('seed round is funding',
          is_funding_story({'title': 'Ellis emerges from stealth with $10M seed round'}),
          True)
    check('series b is funding',
          is_funding_story({'title': 'Simile raises $200M Series B at $2B valuation'}),
          True)
    check('chinese funding',
          is_funding_story({'title': 'Zenity 完成 1.25 亿美元 C 轮融资，由 Norwest 领投'}),
          True)
    check('product launch is not funding',
          is_funding_story({'title': 'Perplexity launches Projects',
                            'summary': 'Projects are hubs for ongoing work.'}), False)
    check('launch mentioning a past raise is not funding',
          is_funding_story({'title': 'Acme ships its coding agent',
                            'summary': 'Acme, a team led by a former Google '
                                       'engineer, shipped the agent today.'}), False)

    # Dedup
    same_url = [
        {'title': '@a: startup ships thing', 'description': 'A ships thing',
         'source_url': 'https://news.site/a?utm_source=twitter'},
        {'title': '@b: different words entirely here', 'description': 'B says ships',
         'source_url': 'https://news.site/a'},
    ]
    unique, stats = dedupe(same_url)
    check('dedup same url', (len(unique), stats['same-url']), (1, 1))

    near = [
        {'title': '@a: Harvey raises $300M Series E led by Kleiner Perkins',
         'description': 'Harvey raises $300M Series E led by Kleiner Perkins', 'url': ''},
        {'title': '@b: Harvey raises $300M Series E led by Kleiner Perkins at $8B',
         'description': 'Harvey raises $300M Series E led by Kleiner Perkins at $8B valuation',
         'url': '', 'source_url': 'https://news.site/harvey'},
    ]
    unique, stats = dedupe(near)
    check('dedup near text', len(unique), 1)
    check('dedup keeps linked version', news_link(unique[0]), 'https://news.site/harvey')

    distinct = [
        {'title': '@a: Harvey raises $300M Series E', 'description': 'Harvey raises 300M'},
        {'title': '@b: Cursor launches agent mode', 'description': 'Cursor launches agent mode'},
    ]
    check('dedup keeps distinct', len(dedupe(distinct)[0]), 2)

    # ── same-story dedup ─────────────────────────────────────────────────────
    # Every case below is taken from real collected posts. The true duplicates
    # sit at 0.27-0.45 word overlap and the false pairs sit at 0.30-0.33, so
    # word overlap alone cannot separate them — shared markers are what does.
    def pair(a, b):
        return dedupe([{'title': '@x: ' + a[:60], 'description': a},
                       {'title': '@y: ' + b[:60], 'description': b}])[0]

    # TRUE: the same launch, announced by the company and written up by press
    check('same launch, two write-ups', len(pair(
        'Google released Nano Banana 2 Lite, its fastest and most cost-efficient '
        'Gemini Image model, alongside Gemini Omni Flash in the Gemini API.',
        'We are shipping 2 major releases: Nano Banana 2 Lite, our fastest and '
        'cheapest Gemini Image model, plus Gemini Omni Flash in the Gemini API.')), 1)
    # TRUE: the same regulatory event, reported and announced
    check('same event, two accounts', len(pair(
        'Anthropic has received notice that the export controls on Claude Fable 5 '
        'and Mythos 5 have been lifted by the Department of Commerce.',
        'We have received notice that the Department of Commerce has lifted export '
        'controls on Claude Fable 5 and Mythos 5, effective immediately.')), 1)
    # TRUE: one funding round, two wordings — carried by name + figure
    check('same round, two wordings', len(pair(
        'Sierra raised $350 million led by Greenoaks.',
        'Sierra lands a $350M round at a $10B valuation, Greenoaks leading.')), 1)

    # FALSE: two different products launching on the same platform
    check('two launches, one platform', len(pair(
        'Nano Banana 2 Lite is now available in Runway. Create images at warp '
        'speed without compromising on quality with this agent.',
        'Generate and edit video with Gemini Omni Flash, now in Runway. Start '
        'with a prompt, image or video and let the agent work.')), 2)
    # FALSE: an announcement, and a quote-tweet reacting to it
    check('announcement vs reaction', len(pair(
        'Claude Fable 5 will be available again globally tomorrow after a series '
        'of productive conversations with the Department.',
        'In the near term, some routine tasks like coding and debugging will fall '
        'back to Opus 4.8, which is a big deal for costs.')), 2)
    # FALSE: two different rounds sharing only funding boilerplate
    check('two different rounds', len(pair(
        'Sierra raised $350M Series C led by Kleiner Perkins.',
        'Harvey raised $200M Series C led by Sequoia Capital.')), 2)
    # FALSE: same company, two unrelated stories in one window
    check('one company, two stories', len(pair(
        'Aseon Labs raised $10 million from Crane Venture Partners for robotaxi '
        'cleaning pitstops.',
        'Aseon Labs named a new chief technology officer, hiring from the '
        'autonomous trucking industry.')), 2)

    # URL spellings of one article are one article
    check('http vs https', canonical_url('http://news.site/a'),
          canonical_url('https://news.site/a'))
    check('amp and mobile', canonical_url('https://m.news.site/a/amp'),
          canonical_url('https://news.site/a'))
    check('dedup across url spellings', len(dedupe([
        {'title': 'a', 'description': 'Startup ships thing',
         'source_url': 'http://www.news.site/a/?utm_source=x'},
        {'title': 'b', 'description': 'Different words about the same thing',
         'source_url': 'https://news.site/a/amp'},
    ])[0]), 1)

    # Summarizer-assigned exclusions
    check('excluded subject',
          bool(excluded_by_summary({'subject_type': 'frontier_lab'})), True)
    check('excluded opinion',
          bool(excluded_by_summary({'content_type': 'opinion'})), True)
    check('startup kept',
          excluded_by_summary({'subject_type': 'startup', 'content_type': 'news'}), '')
    # Unclassified (older run, or added by hand): judged on the text itself
    check('unclassified news kept',
          excluded_by_summary({'title': 'Sierra raises $350M Series C led by Greenoaks',
                               'description': 'Sierra raises $350M Series C led by Greenoaks'}), '')
    check('unclassified chatter dropped',
          bool(excluded_by_summary({'title': '@vc: Go Birds', 'description': 'Go Birds'})), True)

    # Hand-picked articles bypass the editorial filters entirely
    wechat = {'source': 'WeChat', 'title': '某高管表示行业将迎来变化',
              'description': '某高管在采访中表示，行业将迎来变化。',
              'url': 'https://mp.weixin.qq.com/s/abc', 'content_type': 'opinion'}
    swept = {'source': 'X/Twitter', 'title': '@a: exec says things',
             'description': 'The CEO said the industry will change.',
             'url': 'https://x.com/a/status/1'}
    with contextlib.redirect_stdout(io.StringIO()):
        kept_hp = curate([wechat, swept], require_source=True)
    check('hand-picked WeChat kept', len(kept_hp), 1)
    check('hand-picked is the one kept', kept_hp[0].get('source'), 'WeChat')
    check('statement type dropped',
          excluded_by_summary({'content_type': 'statement'}), 'a statement, not an event')
    # The backstop for posts kept at collection on a funding event alone
    check('non-AI company dropped',
          excluded_by_summary({'category': NON_AI_CATEGORY, 'content_type': 'news'}),
          'not an AI company')
    check('AI company kept',
          excluded_by_summary({'category': '行业动态', 'content_type': 'news',
                               'subject_type': 'startup'}), '')

    # Corroboration: does the proposed source article back the post?
    post = 'Aseon Labs raised $10 million from Crane Venture Partners to build ' \
           'robotaxi cleaning and charging pitstops.'
    right = ('Aseon Labs, a Y Combinator company, has raised $10 million led by '
             'Crane Venture Partners. The startup builds pitstops that clean and '
             'charge robotaxis between rides, and plans to expand to more cities.')
    wrong = ('Waymo expanded its robotaxi service to three new metro areas this '
             'week, adding several hundred vehicles to its fleet.')
    check('corroborates right article', corroborates(post, right)[0], True)
    check('rejects wrong article', corroborates(post, wrong)[0], False)
    check('rejects empty page', corroborates(post, '')[0], False)

    # require_source drops what no publication covered
    with_link = {'title': 'a', 'description': 'Harvey raised a seed round from Kleiner',
                 'source_url': 'https://news.site/a', 'content_type': 'news'}
    without = {'title': 'b', 'description': 'Cursor launched an iOS app in public beta',
               'url': 'https://x.com/a/status/1', 'content_type': 'news'}

    with contextlib.redirect_stdout(io.StringIO()):
        kept_req = curate([with_link, without], require_source=True)
        kept_open = curate([with_link, without], require_source=False)
    check('require_source drops unlinked', len(kept_req), 1)
    check('require_source off keeps both', len(kept_open), 2)

    if failures:
        print(f"✗ {len(failures)} self-test failure(s):")
        for f in failures:
            print(f"    {f}")
        return 1
    print("✓ news_filters: all self-tests passed")
    print(f"  {len(labs)} excluded companies loaded from {os.path.basename(DEFAULT_LABS_FILE)}")
    return 0


if __name__ == '__main__':
    sys.exit(_test())

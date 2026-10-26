#!/usr/bin/env python
"""
Seed the Tester5 account with tags and notes for manual testing.

Creates 35 new tags (top-level plus sub-tags; ~40 total including the
existing ones) and attaches ~250 notes to each of the first three
tags alphabetically: 'Assurance', 'Atonement', 'Attributes of God'.

Note mix per heavy tag (250 notes):
- 180 notes: verse references + note text
-  35 notes: verse references only (empty note_text)
-  27 notes: text only (no verse references)
-   8 notes: completely empty (no text, no verses) — edge cases

Usage:
    python scripts/seed_tester5_data.py
"""

import os
import random
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'bible_research.settings')

import django  # noqa: E402

django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from annotations.models import Note, NoteVerse, Tag  # noqa: E402
from bible.models import Verse  # noqa: E402

User = get_user_model()
random.seed(20261021)

USERNAME = 'Tester5'
NOTES_PER_HEAVY_TAG = 250

# New top-level tags. The first three names sort alphabetically before
# every existing Tester5 tag ('Bulk Test', 'CKx0eqgp', ...), so they
# land at the top of the tag list.
TOP_LEVEL_TAGS = [
    'Assurance',
    'Atonement',
    'Attributes of God',
    'Covenants',
    'Discipleship',
    'Eschatology',
    'Faith',
    'Grace',
    'Holiness',
    'Holy Spirit',
    'Kingdom of God',
    'Parables',
    'Prayer',
    'Prophecy',
    'Repentance',
    'Resurrection',
    'Sermon on the Mount',
    'Wisdom',
    'Worship',
]

# (parent name, child name) — one level of nesting.
SUB_TAGS = [
    ('Attributes of God', 'Love'),
    ('Attributes of God', 'Holiness of God'),
    ('Attributes of God', 'Sovereignty'),
    ('Attributes of God', 'Justice'),
    ('Prayer', 'Intercession'),
    ('Prayer', 'Thanksgiving'),
    ('Prayer', "Lord's Prayer"),
    ('Prophecy', 'Messianic'),
    ('Prophecy', 'Fulfilled in Christ'),
    ('Holy Spirit', 'Fruit of the Spirit'),
    ('Holy Spirit', 'Gifts of the Spirit'),
    ('Eschatology', 'Second Coming'),
    ('Covenants', 'New Covenant'),
    ('Covenants', 'Abrahamic Covenant'),
]

# (grandparent, parent, child) — a second level of nesting.
SUB_SUB_TAGS = [
    ('Prophecy', 'Messianic', 'In the Psalms'),
    ('Prophecy', 'Messianic', 'In Isaiah'),
]

HEAVY_TAGS = ['Assurance', 'Atonement', 'Attributes of God']

# (book, chapter) pools roughly matching each heavy tag's theme.
VERSE_POOLS = {
    'Assurance': [
        ('Romans', 8), ('John', 10), ('1 John', 5), ('Ephesians', 1),
        ('Philippians', 1), ('John', 5), ('2 Timothy', 1),
        ('Hebrews', 6), ('1 Peter', 1), ('Psalms', 23),
    ],
    'Atonement': [
        ('Isaiah', 53), ('Leviticus', 16), ('Hebrews', 9),
        ('Hebrews', 10), ('Romans', 3), ('Romans', 5),
        ('1 Peter', 2), ('Mark', 10), ('1 John', 2),
        ('2 Corinthians', 5),
    ],
    'Attributes of God': [
        ('Exodus', 34), ('Psalms', 139), ('Psalms', 145),
        ('Isaiah', 40), ('Job', 38), ('Job', 42), ('1 John', 4),
        ('Revelation', 4), ('Psalms', 90), ('Deuteronomy', 32),
    ],
}

NOTE_TEXTS = [
    "The verb tense here matters — a finished act, not an "
    "ongoing process. Comforting.",
    "Cross-reference {ref}: same theme, different covenant "
    "context. Worth a side-by-side read.",
    "Spurgeon's take on this passage: the promise is "
    "conditional on abiding, not on feeling.",
    "Notice the contrast pair in this chapter — light vs "
    "darkness sets up the whole argument.",
    "Why does the author repeat this phrase three times? "
    "Possibly a liturgical echo.",
    "Application: this should change how I pray for "
    "Andrejs — less anxiety, more trust.",
    "The Hebrew word behind 'steadfast' is chesed — "
    "covenant loyalty, not mere kindness.",
    "Reading this in LVS (Latvian) the imperative hits "
    "harder than the English.",
    "This verse undoes the idea that God is reluctant to "
    "forgive. He runs toward the repentant.",
    "Sermon notes (Pastor Ivars, 12 Oct): context is exile; "
    "the promise presumes discipline.",
    "Check {ref} — Paul seems to be riffing on this exact "
    "imagery.",
    "The chiastic structure here centres on v{verse}, not "
    "the verse everyone quotes.",
    "Hard passage. Calvin and Wright land in opposite "
    "places; leaning Calvin on this one.",
    "Memorize this week. The logic: if God gave the "
    "greater gift, why doubt the lesser?",
    "Note the audience shift — from crowds to disciples. "
    "The teaching sharpens in private.",
    "Greek: apolutrosis — a ransom price paid. Debt "
    "language, not just 'rescue'.",
    "This refutes the claim that assurance breeds "
    "laziness; here assurance fuels endurance.",
    "Link to {ref}. Both passages use adoption language "
    "to ground security.",
    "The imperative sits inside an indicative — we "
    "become what we already are in Christ.",
    "Quoted in Sunday school; the kids grasped the "
    "shepherd imagery faster than the adults.",
    "TBD: compare ESV vs NIV rendering of the last "
    "clause. One softens it noticeably.",
    "Prophecy → fulfilment chain starts here. Trace to "
    "Matthew's formula quotations.",
    "The 'therefore' at the start carries the whole "
    "weight of the previous chapter.",
    "Liturgical use: this is the text behind the "
    "benediction we close with.",
    "Strange verse at first read; the cultural backdrop "
    "(kinsman-redeemer) makes it click.",
    "Psalm of ascent — sung on the way up to Jerusalem. "
    "Read it as pilgrimage language.",
    "The fear-of-the-LORD motif again; ties into the "
    "wisdom thread I'm tracking.",
    "Not a promise of ease — a promise of presence. "
    "Important distinction.",
    "Augustine cites this in Confessions IX; his reading "
    "still holds up.",
    "The passive voice is deliberate: God is the actor, "
    "we are the acted-upon.",
    "Margin note from my old Bible: 'preached on this "
    "2019-03-17 — half the room wept'.",
    "Eschatological horizon here — not just immediate "
    "comfort but final restoration.",
    "The triple 'holy' is the only attribute of God "
    "raised to the superlative in Scripture.",
    "Lexical note: the word translated 'justice' pairs "
    "with 'righteousness' as a hendiadys.",
    "This is the hinge verse of the whole book — "
    "everything pivots on it.",
    "Compare the parallel account: the details Luke adds "
    "change the emphasis entirely.",
    "Prayer response: 'Keep me from treating grace as a "
    "technicality.'",
    "The covenant formula 'I will be their God' threads "
    "from Genesis to Revelation.",
    "Jotting this before I forget — the sacrificial "
    "system prefigures, it doesn't merely symbolize.",
    "Short thought: obedience here is relational, not "
    "transactional.",
    "Question for small group: does 'all' here really "
    "mean all, or all kinds?",
    "The historical context (post-exilic community) "
    "explains the urgency in the tone.",
    "Verse to send to Mareks — he's been wrestling with "
    "exactly this.",
    "The Septuagint renders this with a stronger verb; "
    "the NT quotes the LXX reading.",
]


def ref_str(verse):
    return f"{verse.book} {verse.chapter}:{verse.verse}"


def load_verse_pool(tag_name):
    """Return {chapter_key: [Verse, ...]} for the tag's pool."""
    pool = {}
    for book, chapter in VERSE_POOLS[tag_name]:
        verses = list(
            Verse.objects.filter(book=book, chapter=chapter)
            .order_by('verse')
        )
        if not verses:
            print(f'  WARNING: no verses for {book} {chapter}')
            continue
        pool[(book, chapter)] = verses
    return pool


def pick_verses(pool):
    """Pick 1-4 verses, all within a single chapter."""
    key = random.choice(list(pool.keys()))
    verses = pool[key]
    roll = random.random()
    if roll < 0.60:
        return [random.choice(verses)]
    if roll < 0.90:
        start = random.randrange(len(verses))
        end = min(start + random.randint(2, 4), len(verses))
        return verses[start:end]
    return random.sample(verses, k=min(2, len(verses)))


def make_text(picked):
    template = random.choice(NOTE_TEXTS)
    text = template
    if '{ref}' in text:
        v = random.choice(picked) if picked else None
        other = (
            ref_str(v) if v
            else f"Romans {random.randint(3, 12)}:"
                 f"{random.randint(1, 30)}"
        )
        text = text.replace('{ref}', other)
    if '{verse}' in text:
        v = random.choice(picked) if picked else None
        text = text.replace(
            '{verse}', str(v.verse if v else random.randint(2, 20))
        )
    return text


def seed_tags(user):
    created = {}
    for name in TOP_LEVEL_TAGS:
        tag, was_created = Tag.objects.get_or_create(
            user=user, name=name, parent_tag=None
        )
        created[name] = tag
        if was_created:
            print(f'  + tag: {name}')
    for parent_name, child_name in SUB_TAGS:
        tag, was_created = Tag.objects.get_or_create(
            user=user, name=child_name,
            parent_tag=created[parent_name],
        )
        created[child_name] = tag
        if was_created:
            print(f'  + sub-tag: {parent_name} > {child_name}')
    for gp_name, parent_name, child_name in SUB_SUB_TAGS:
        tag, was_created = Tag.objects.get_or_create(
            user=user, name=child_name,
            parent_tag=created[parent_name],
        )
        if was_created:
            print(
                f'  + sub-tag: {gp_name} > {parent_name}'
                f' > {child_name}'
            )
    return created


def seed_notes(user, tags):
    for tag_name in HEAVY_TAGS:
        tag = tags[tag_name]
        existing = Note.objects.filter(user=user, tag=tag).count()
        to_create = NOTES_PER_HEAVY_TAG - existing
        if to_create <= 0:
            print(
                f'  {tag_name}: already has {existing} notes, '
                f'skipping'
            )
            continue

        pool = load_verse_pool(tag_name)
        notes, links = [], []
        # Split: 180 text+verses, 35 verses-only, 27 text-only,
        # 8 fully empty.
        kinds = (
            ['text_verses'] * 180
            + ['verses_only'] * 35
            + ['text_only'] * 27
            + ['empty'] * 8
        )[:to_create]
        random.shuffle(kinds)

        for i, kind in enumerate(kinds):
            picked = [] if kind in ('text_only', 'empty') \
                else pick_verses(pool)
            text = '' if kind in ('verses_only', 'empty') \
                else make_text(picked)
            note = Note(
                user=user,
                tag=tag,
                note_text=text,
                public=(random.random() < 0.10),
                tag_position=float(existing + i + 1),
            )
            notes.append(note)
            links.extend(
                NoteVerse(note=note, verse=v) for v in picked
            )

        # bulk_create populates PKs on Postgres, so the NoteVerse
        # objects built above resolve once notes are inserted.
        Note.objects.bulk_create(notes, batch_size=500)
        NoteVerse.objects.bulk_create(links, batch_size=1000)
        print(
            f'  {tag_name}: created {len(notes)} notes, '
            f'{len(links)} verse links'
        )


def main():
    user = User.objects.get(username=USERNAME)
    print(f'Seeding data for {user} (id={user.id})')

    print('Creating tags...')
    tags = seed_tags(user)
    total = Tag.objects.filter(user=user).count()
    print(f'Total tags for {USERNAME}: {total}')

    print('Creating notes...')
    seed_notes(user, tags)

    print('Done. Final counts:')
    for t in Tag.objects.filter(user=user).order_by('name')[:5]:
        print(f'  {t.name}: {t.notes.count()} notes')
    print(
        f'  Total notes: '
        f'{Note.objects.filter(user=user).count()}'
    )


if __name__ == '__main__':
    main()

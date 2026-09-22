# UX bar

Every pixel must answer one question: **why is it here?** If it does not change
what the person understands or does next, it is gone.

## Rules

1. **One job per screen.** Name it before designing it. Everything on the screen
   serves that job or leaves.
2. **Value first.** The number, date or verdict is the largest thing. Labels are
   quiet. Explanations appear on tap, not by default.
3. **No unearned text.** No helper copy, subtitles, footers, empty-state essays,
   capability lists or narration of what the system is doing, unless its absence
   would cause a mistake.
4. **No unearned fields.** IDs, revisions, confidence scores, raw keys, review
   dates and provenance detail live behind a tap. The API returns only what the
   view renders.
5. **One primary action.** One filled button per screen. Secondary actions are text.
6. **Honest states.** Unknown is shown as unknown in one short phrase, never as
   zero, a spinner or a placeholder chart. Failures say what broke and the one
   way forward.
7. **Motion and colour are meaning.** Colour follows the Dot contract
   (vermilion = action, cobalt = live, ink = settled). No decoration.
8. **Production finish.** 44px targets, no overflow at 375px, keyboard and
   screen-reader complete, sentence case, es-MX and en both first-class, numbers
   formatted for the locale with tabular figures.

## Review gate

A UI change merges only after a screenshot review at 390px and 1280px in which
each visible element has been challenged against rule 1, and anything that
could not justify itself was removed.

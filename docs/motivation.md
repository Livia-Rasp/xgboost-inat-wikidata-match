# Why this project exists

iNaturalist's taxon pages resolve their Wikipedia "About" summary by first trying
`hub.toolforge.org/P3151:{taxon_id}?lang=en` — a redirect service that follows the iNat ID to
its Wikidata item, then to *that item's* Wikipedia sitelink (confirmed in iNat's own source:
[`taxon_describers/wikipedia.rb`](https://github.com/inaturalist/inaturalist/commit/29ec770e47dec9de4b9e65e1293cb4c1e49ab7de),
live since 2020). Only when that lookup fails does it fall back to naive name-string matching
against Wikipedia article titles, with no kingdom or rank disambiguation. That is how
[`Subgenus Absidia`](https://www.inaturalist.org/taxa/552989-Absidia), an animal subgenus under
genus *Podistra*, ends up displaying the Wikipedia summary for an unrelated fungal genus of the
same name.

Checking the live Wikidata data behind that specific case turned up something more interesting
than a missing link. The correctly disambiguated Wikidata item (`Q18510042`) already carries a
P3151 statement for this exact taxon, but has zero Wikipedia sitelinks, so there is nothing for
the redirect to land on. A second Wikidata item for the same real-world taxon (`Q50746010`,
described as "subgenus of Podistra") has no P3151 yet, which is why it is one of the ambiguous
cases in this project's own gold set. Its two candidates are exactly the fungus (iNat `552980`)
and the correct animal subgenus (iNat `552989`).

Resolving it would not fix that particular page. The actual gap there is a Wikidata
duplicate-item merge plus a missing English article, not a missing link. What the source dive
did confirm, rather than assume, is that the P3151-driven redirect is live and works today
wherever the correctly disambiguated Wikidata item has a Wikipedia sitelink waiting on it.

Producing exactly that — correct, disambiguated P3151 links for the ambiguous name-collision
cases naive matching gets wrong — is what this project is for.

## Where the ambiguous cases come from

[wikidata-inat-checker](https://github.com/Livia-Rasp/wikidata-inat-checker) scans Wikidata taxa
against a local index of iNaturalist's open-data taxon dump. Most items resolve on a unique name
match. A small fraction (~0.57% of scanned names) hit more than one plausible iNat taxon and get
written to `output/links-ambiguous.html` for a human to resolve by hand. That file is the review
queue this classifier exists to shrink.

Hemihomonyms are the reason the queue is not empty. *Prunella* is both a mint (Lamiaceae) and a
bird (Prunellidae). *Oenanthe* is both a wheatear and a water-dropwort. The two candidates are
identical as strings and differ only in ancestry, so any rule that stops at the first name match
is wrong roughly half the time on exactly the cases that need it most.

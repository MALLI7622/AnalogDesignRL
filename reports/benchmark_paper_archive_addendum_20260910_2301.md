# Paper-archive retrieval addendum — 2026-09-10, 23:01 UTC

A bounded retrieval pass ran from **22:49 to 23:01 UTC** for the four original papers whose complete PDFs were missing from the local research archive. **No additional original paper PDF or original circuit figure was recovered.** The pass added retrievable bibliographic evidence, documented failed links, and rejected one misleading PDF result. Existing full-text and figure-review claims should remain at their previous levels.

Only new assets under `runs/research_sources_20260910/paper_archive_addendum_2249/` and this new addendum were written. Frozen benchmark modules, domains, candidate tasks, source netlists, previous reports, and simulation caches were not changed. No simulation was run.

## Retrieval outcome

| Original source paper | Concrete checks in this pass | Evidence status after this pass |
|---|---|---|
| Fan, Mishra, Sánchez-Sinencio, **Single Miller Capacitor Frequency Compensation Technique for Low-Power Multistage Amplifiers**, JSSC 2005, DOI `10.1109/JSSC.2005.843602` | The known [PDF mirror](https://picture.iczhiku.com/resource/eetop/WYKhwFHGQpjORCXn.pdf) returned HTTP 403 to direct download. Cached original text remained readable, but rendering page 587 containing Fig. 6 failed. The [author bibliography](https://people.engr.tamu.edu/s-sanchez/Publication_v4.html) and [official lab list](https://amsc.tamu.edu/journals.html) confirm the journal citation and point to IEEE document 1408078. OpenAlex exposed the historical TAMU file `http://amsc.tamu.edu/SIS/Publications/pub/jounal/2005_4.pdf`; that URL, its HTTPS form, and one plausible migrated path returned 404. CiteSeerX's indexed copy also returned 404. Ordinary IEEE PDF access returned 418. | Original-paper text and released AnalogGym schematic previously reviewed. **Original figure remains uninspected; original PDF remains absent locally.** Added author-hosted bibliographic provenance and a specific dead historical source path. |
| Peng and Sansen, **AC Boosting Compensation Scheme for Low-Power Multistage Amplifiers**, JSSC 2004, DOI `10.1109/JSSC.2004.835811` | The known [PDF mirror](https://picture.iczhiku.com/resource/eetop/sHItHoErkwWsfMBC.pdf) returned HTTP 403. Cached original text remained available, but rendering the original Fig. 2 page failed. [Publisher-deposited Crossref metadata](https://api.crossref.org/works/10.1109%2FJSSC.2004.835811) identifies IEEE document 1347342; its ordinary PDF access returned 418. OpenAlex supplied no repository PDF location. | Original-paper text and released AnalogGym schematic previously reviewed. **Original figure remains uninspected; original PDF remains absent locally.** Publisher metadata is now archived. |
| Ramos and Steyaert, **Three Stage Amplifier With Positive Feedback Compensation Scheme**, CICC 2002, DOI `10.1109/CICC.2002.1012833` | The [official MICAS bibliography](https://micas.esat.kuleuven.be/publications?member=00014436&page=3&type=conference-proceeding) confirms the conference citation, pages 333–336, and LIRIAS record 1134946. [LIRIAS](https://lirias.kuleuven.be/1134946) yielded a JavaScript library-discovery shell rather than paper content. A public-record endpoint attempt returned 400. OpenAlex listed no repository PDF; ordinary IEEE PDF access returned 418. The previously reviewed [public OCR transcript](https://www.scribd.com/document/241079749/01012833-Technical-paper) remains the text evidence. | OCR/transcript and released schematic previously reviewed. **No upgrade to original full-PDF or original-figure review.** Added institutional bibliographic confirmation. The distinct 2004 journal sequel is not substituted for this 2002 paper. |
| Song Guo and Hoi Lee, **Dual Active-Capacitive-Feedback Compensation for Low-Power Large-Capacitive-Load Three-Stage Amplifiers**, JSSC 2011, DOI `10.1109/JSSC.2010.2092994`; release name `Song_DACFC_Pin_3` | [Crossref](https://api.crossref.org/works/10.1109/JSSC.2010.2092994) identifies IEEE document 5671500. OpenAlex and Semantic Scholar report closed access without a repository PDF URL. The [author's public journal directory](https://personal.utdallas.edu/~hoilee/publications/Journals/) contains eight older PDFs and no DACFC paper; the [author bibliography](https://personal.utdallas.edu/~hoilee/publication.html) lists the 2011 paper without a download link. Ordinary IEEE PDF access returned 418. Author-group/dissertation and academic repository searches did not recover the original. | Original abstract and author bibliography, plus released netlist/schematic, remain the available evidence. **Original full text and original schematic are still unavailable.** No 2007 conference precursor or later analytical paper is treated as the original 2011 paper. |

OpenAlex's absence of an indexed repository copy is retrieval evidence, not proof that no lawful copy exists anywhere. No login bypass, bot-check bypass, author message, or access-control circumvention was attempted. Publisher metadata links designated for similarity checking were not treated as open-access permissions or alternative retrieval routes.

## A real PDF that is not the original paper

A [University of Shanghai for Science and Technology library PDF](https://library.usst.edu.cn/_upload/article/files/98/56/3e23e5e345119c4502fc4d6b79d6/2495b03d-efa4-4d7a-b439-3292b34a4614.pdf) was downloaded as `song_jssc_issue_usst.pdf`. It is a real, unencrypted **two-page scanned table of contents for the February 2011 JSSC issue**. Both pages were rendered and visually inspected. The first page lists the Guo–Lee paper beginning at page 452; the file contains neither that paper's text nor its schematic. Its empty extracted text is consistent with scanned page images.

SHA256: `d3e5100a450568b8ca7e39e73177e8a62a8c1b69c27b9ba04c165400b1be86d3`. This is retained as supporting bibliographic evidence and explicitly excluded from the original-paper count.

## Archive and count discipline

The complete retrieval log and hashes are in [archive_retrieval_manifest.json](../runs/research_sources_20260910/paper_archive_addendum_2249/archive_retrieval_manifest.json), SHA256 `9152471b881d440ac7ee598d526e2f1ec64a4cce63c39ce621dc2ff526bdcd01`. The dedicated [Song retrieval manifest](../runs/research_sources_20260910/paper_archive_addendum_2249/song_retrieval_manifest.json) records the original DOI, failed/full-text searches, PDF rejection, and all Song asset hashes. The combined manifest indexes nineteen new supporting files, including downloaded metadata, author/library pages, the issue-contents PDF, and its page images. HTTP error responses were not saved as alleged papers.

The ten core circuit netlists draw on **eight original source papers**:

1. AutoCkt 2020.
2. Fan SMC 2005.
3. Leung–Mok 2001, shared by NMCNR, DFCFC1, and DFCFC2.
4. Hoi Lee–Mok AFFC 2003.
5. Peng–Sansen ACBC 2004.
6. Peng et al. IAC 2011.
7. Ramos–Steyaert PFC 2002.
8. Guo–Lee DACFC 2011.

Four of these original source papers already have locally inspected complete PDFs and original schematic evidence: AutoCkt, Leung–Mok, Hoi Lee–Mok, and IAC. Fan and ACBC retain original full-text review without original-figure inspection; Ramos retains OCR/transcript review; Guo–Lee retains abstract/bibliography review. This retrieval pass changes none of those levels.

The AnalogGym infrastructure paper, relevant conference precursors, later comparison studies, and the recovered journal contents are **additional supporting sources**, not extra original source papers. The project remains a nominal SKY130 sizing benchmark on published circuit architectures; neither access to a paper nor bibliographic confirmation establishes reproduction of its original device technology, measurements, or performance.

All downloaded copies and rendered pages remain research assets. Do not include them in learner packages or redistribute them with the benchmark. The existing netlist license notice applies to the released circuit artifacts, not to copyrighted journal papers or their figures.

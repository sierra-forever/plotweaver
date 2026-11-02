# Plotweaver

<img src="plotweaver/resources/icon.png" width="96" alt="Plotweaver icon">

Novel planning software angled toward men's adventure and haremlit, which ties in with
[FocusWriter](https://gottcode.org/focuswriter/) so you can get from planning to writing quickly.

It's a desktop app for Windows and Linux. Everything is stored as plain Markdown files with YAML
properties, and the format is compatible with Obsidian, so a project folder opens there too.

## Download

Get the latest version from the [Releases page](../../releases):

| | |
|---|---|
| **Windows installer** | `Plotweaver-<version>-Setup.exe`. Installs for just you (no admin needed) with a Start menu entry. Uninstall from Windows Settings like any other app. |
| **Windows portable** | `Plotweaver-<version>-windows-portable.zip`. Unzip anywhere (a USB stick or a synced folder) and run `Plotweaver.exe`. Settings stay in a `data` folder beside it. Nothing is installed. |
| **Linux** | `Plotweaver-<version>-x86_64.AppImage`. Make it executable (`chmod +x`) and run it. |

Windows may show a SmartScreen warning the first time ("Windows protected your PC"), because the
app isn't code-signed. Click **More info → Run anyway**.

Both Windows downloads include a sample project, "The Ember Crown", a short example novel that
uses every feature. The portable zip has it in a `Sample Project` folder beside `Plotweaver.exe`.
The installer puts it in `Documents\Plotweaver\Sample Project`. When you upgrade, the installer
offers to replace a sample from an older version, and it never touches a sample from the same
version or a folder of your own. On any version, **File → Open Sample Project** (or the button on
the start screen) opens it, and creates it in `Documents/Plotweaver` if it isn't there yet.

The portable and installed versions are the same program. The only difference is the
`portable.txt` file in the portable folder, which tells Plotweaver to keep its settings next to
itself instead of in your user profile. Your novels are never stored with the app either way.

## Run from source

```
pip install -r requirements.txt
python main.py
```

On Windows you can also double-click `run.bat`. On Linux you can run `./run.sh`.
Python 3.10 or newer is required.

## The project folder

```
My Novel/
  project.md                               title, author, target word count
  outline/
    Part One/Part One.md                   a part (a "folder note")
    Part One/The Pass Road/The Pass Road.md    a chapter
    Part One/The Pass Road/Gate at Dusk.md     a scene
  characters/   places/   codex/   notes/
  images/                                  portraits and gallery images
  drafts/                                  suggested home for your prose files
  .trash/                                  deleted items go here, not to oblivion
```

* **Names are file names.** `[[Sera Vale]]` links to `characters/Sera Vale.md`. Any
  `aliases:` entry works too, so `[[Sera]]` resolves to the same note. Every name must be unique.
* **Renaming updates every link** across the whole project.
* **Order lives in an `order:` property**, so reordering scenes never renames files or breaks links.
* **Edit files anywhere.** Changes made in another editor, Obsidian, or git show up
  automatically. Unknown properties are kept and listed under *Other properties*.

## Using it

| | |
|---|---|
| New scene / chapter | `Ctrl+N` / `Ctrl+Shift+C`. A new item goes after whatever is selected. |
| New character / place / codex | `Ctrl+Shift+H` / `Ctrl+Shift+L` / `Ctrl+Shift+X` |
| Reorder | Drag in the outline, or `Alt+Up` / `Alt+Down` |
| Rename | Edit the big title, or press `F2` |
| Link | Type `[[` in the notes for name suggestions. `Ctrl+click` a link to follow it. |
| Go to anything | `Ctrl+P` |
| Editor / Corkboard / Matrix / Relationships | `Ctrl+1` / `Ctrl+2` / `Ctrl+3` / `Ctrl+4` |
| Filter by tag | `Ctrl+T` |
| Open the scene's draft | `Ctrl+E` |

**View → Theme** switches between Light and Dark (Dark by default). The app uses its own palettes,
so it looks the same whatever your desktop's color scheme is.

The sidebar shows each scene's draft word count, with totals on chapters and parts. They update
whenever you save a draft in your writing app.

The right-hand panel shows **Linked mentions**: every note that links to the one you're viewing.
For a character, that's every scene they appear in. **Unresolved links** lists names
you've linked that don't have a note yet. Double-click one to create it.

### Chapters always start with a scene

**New Chapter** (`Ctrl+Shift+C`) asks for the chapter's name and its first scene's name together.
The chapter name is prefilled as "Chapter N" for the position it will take, so usually you just
type the scene name and press Enter. If you insert "Chapter 4" before an existing Chapter 4, tick
the box to renumber the later chapters and make room. A scene can't have the same name as its
chapter (they'd be the same file). Deleting a chapter's only scene asks whether to delete the
whole chapter instead.

### Portraits

A character's page has a portrait box on the right. Drop an image onto it (or use **Choose…**).
Drop extra images below it into the **gallery**, which is handy for keeping alternate generations.
Double-click a gallery image, or right-click it and choose **Use as portrait**, to promote it. The
old portrait moves into the gallery.

Images are copied into the project's `images/` folder and named after the character
("Sera Vale.png", "Sera Vale 2.png"), so the project stays self-contained. Dropping the same
picture twice reuses the existing copy. They're stored as Obsidian-style links
(`portrait: "[[Sera Vale.png]]"`), so Obsidian finds them too. Portraits also show as small
avatars in the **In this scene** panel, and when you hover over a character in the sidebar.

### Characters at a glance

Characters have a free-text **Appearance** box and a short **Key traits** list (e.g. `silver hair,
green eyes, 5'2"`). Traits autocomplete from the ones you've already used, so wording stays
consistent. When you're on a scene, the **In this scene** panel lists the POV and everyone present
with their role and traits. Hovering over a character in the sidebar shows the same details.

### Tags

Tag anything with the **Tags** field, or type `#tags` anywhere in the notes (both count, the same as
in Obsidian). Nested tags work too: `#magic` also finds `#magic/fire`. In the sidebar filter, type
`#` (or press `Ctrl+T`) to get tag suggestions. Terms combine: `#flashback sera` finds flashback
scenes that mention Sera in their name or summary. The corkboard and matrix follow the same filter.

### Corkboard (`Ctrl+2`)

Your scenes as index cards in reading order, grouped by part. Each card shows its chapter, the
summary (or the first lines of the notes, in italics, if there's no summary), tags, POV, heat and
draft word count.

* **Drag moves Chapters** (the default) reorders whole chapters. Switch it to **Scenes** to move
  single scenes, including into an empty chapter.
* **Color by** status, POV, thread or tag.
* **Size** goes from titles-only (good for seeing the whole book) up to large cards.
* Cards that don't match the filter fade out rather than disappearing.
* Click a card to select it. Double-click it to open it in the Editor.

### Matrix (`Ctrl+3`)

Chapters (or scenes) down the side and characters across the top. The default columns are the
protagonist and the love interests. Change them with **Characters…**.

* ● means listed in the scene's *Characters* field. ◉ means POV. `2/3` means in two of the
  chapter's three scenes.
* Absences of N rows or more after a character's first appearance are tinted. Chapters with no
  scenes don't count as absences.
* Each column header shows scene count, share of all scenes, and longest absence. Hover over it
  for first appearance.
* Filter by `#tag` to see screen time within, say, only `#flashback` scenes.

### Relationships (`Ctrl+4`)

Add relationships under **Relationships** on a character's note: who with, what kind, an
intensity from 0 to 5, and optionally when it starts and ends. Each one belongs to the pair, so
write it on either character. The other character's note lists it under *On other notes*.

```yaml
relationships:
  - with: "[[Maren Holt]]"
    kind: rival
    until: 4                 # ends at chapter 4
  - with: "[[Maren Holt]]"
    kind: lover
    intensity: 3
    since: 4                 # a chapter number, or a [[chapter]] or [[scene]] link
    note: Slow burn; starts after the fight.
```

The kinds are lover, attraction, harem sister, friend, family, ally, rival, enemy, mentor and
serves, and you can type your own. *Attraction*, *mentor* and *serves* are one-way, read from
the character whose note holds them ("Lysandra is drawn to Sera"), and are drawn with an arrow.
Everything else counts both ways. To show a relationship changing (rivals to lovers), end one
entry and start the next at the same point.

The **Relationships** tab draws them as a web:

* **Harem** puts the protagonist in the middle and the love interests on a ring, so the
  relationships between the women show as lines across it. **Everyone in a relationship** adds
  the rest of the cast on an outer ring, and **All characters** shows every character.
* The **As of** slider shows the web at any chapter. Relationships that haven't started yet are
  hidden, and ones that have ended are dashed (untick **Ended** to hide them).
* A thicker line means a higher intensity. Each link shows how many scenes the pair have shared
  so far. *0 scenes* is in red because those two have never been on the page together.
* **Unlinked pairs** draws a dotted line between characters who share scenes but have no
  relationship written down.
* Drag characters to rearrange them. The layout is remembered per project, and **Reset layout**
  puts the ring back. Click a character or a link to select its note, or double-click to open it.
  **Kinds…** hides kinds you don't want to see. A `#tag` filter limits the scene counts.

### Chapter numbers

If a chapter's name doesn't match its position (say "Chapter 5" is now first), cards and matrix
rows show the real position in front: `1 · Chapter 5`. **Outline → Renumber Chapters** renames
every "Chapter N…" chapter to match its order, and updates links.

### Drafts (FocusWriter, etc.)

Each scene has a **Draft file** field. Use **Link…** to point it at an existing file, or **New…**
to create one in `drafts/`. Supported formats are .txt, .md, .odt, .docx and .rtf. The word
count updates whenever you save in your writing app. **Open** launches FocusWriter if it can be
found, or otherwise the file's default app. You can set a specific command in
*File → Preferences*, for example `flatpak run org.gottcode.FocusWriter`.

*File → Import Drafts as Scenes…* turns a batch of existing draft files into scenes in one go.

## Roadmap

* v2: love-interest arc stages (shown in the matrix)
* v3: story timeline, heat/pacing curve, structure-beat templates, progress dashboard,
  compiled outline export

# Projektsøg

Find enhver projektmappe på få sekunder – uanset om den ligger på din egen pc, på en bærbar
disk eller på en af de andre computere i firmaet.

**Tryk Shift+Mellemrum, skriv et par bogstaver, tryk Enter** – så står mappen åben i
Stifinder.

---

## Hvad gør Projektsøg?

- **Søger overalt på én gang**: lokale drev, bærbare arbejdsdiske og delte mapper på de andre
  pc'er, du har tilføjet (f.eks. STUDIO-PC, KLIPPER-PC, GRAFIK-PC …).
- **Kender jeres projektstruktur**: mapper, der er lavet ud fra skabelonen `1. KUNDENAVN`
  (med `Klip`, `Grafik`, `Musik`, `Speak` …), vises som projekter og kommer øverst.
- **Finder også filer**, f.eks. `FX9_7912.MXF`. Billedsekvenser (`render_0001.exr` …
  `render_4500.exr`) vises som én linje.
- **Husker frakoblede diske og slukkede pc'er**: resultaterne bliver stående (nedtonet), og
  Projektsøg fortæller, hvilken disk du skal tilslutte, eller hvilken pc der skal tændes.
- **DaVinci Resolve**: viser, hvor det åbne projekts optagelser ligger, og åbner mappen med ét
  klik – også direkte fra Resolves menu.
- **Tidsregistrering til fakturering**: tæller automatisk, hvor længe du arbejder på hvert
  Resolve-projekt – fordelt på Edit, Color, Fusion osv. – og eksporterer til Excel.
- **Kører diskret i baggrunden** med et ikon i meddelelsesområdet (ved uret). Scanning sker med
  lav prioritet, så den ikke forstyrrer afspilning i Resolve.
- **Import af kort**: sæt et kort fra FX9, FS7, A7S, DJI eller GoPro i, og Projektsøg foreslår
  projektmappen (eller opretter et nyt projekt ud fra skabelonen), kopierer klippene til
  `Klip\FX9` osv. og kontrollerer hver kopi.
- **Ændrer aldrig dine filer.** Søgningen læser kun navne, størrelser og datoer. Kun når du selv
  importerer et kort eller opretter et projekt, laver Projektsøg nye mapper og kopierer nye filer –
  den overskriver aldrig noget, og den sletter kun fra kortet, når du vælger **Klip** (og først
  når alt er kontrolleret).

## Krav

- Windows 10 eller 11.
- **Python 3.14** eller nyere fra <https://www.python.org/downloads/> (ingen ekstra pakker).
- Microsoft Edge (følger med Windows) – Projektsøgs vindue er et Edge-app-vindue med sin egen
  private profil, så din almindelige browser (f.eks. Chrome) påvirkes ikke.
- Valgfrit: DaVinci Resolve 20 eller nyere.

## Installation

1. Læg Projektsøg-mappen et fast sted på pc'en, f.eks. `C:\Github\Search`. Mappen skal blive
   liggende – programmet kører derfra.
2. Åbn PowerShell i mappen (Shift+højreklik i Stifinder ▸ *Åbn PowerShell-vindue her*) og kør:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install.ps1
   ```

   Installationen kræver ikke administratorrettigheder. Den

   - opretter genvejen **Projektsøg** i Start-menuen,
   - slår **Start med Windows** til (vil du ikke det, så tilføj `-NoAutostart`),
   - kopierer DaVinci Resolve-scriptet til *Workspace ▸ Scripts ▸ Utility*,
   - registrerer Claude-sessionernes Resolve-kø (`koe.py installer`), hvis Davinci-mappen ligger ved
     siden af Projektsøg-mappen eller i `C:\Github\Davinci` (ellers angiv `-Koe "C:\sti\til\koe.py"`) –
     så virker **Byg nu**, telefonen og robotterne i Klippe på den pc (også efter **Opdater nu**),
   - gemmer listen over de andre computere, hvis du angiver `-Hosts` (se nedenfor),
   - starter Projektsøg i baggrunden.

   Findes Python ikke automatisk, så angiv stien:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install.ps1 -Python "C:\sti\til\python.exe"
   ```

   **De andre computere** kan du give med det samme med `-Hosts` – navnene adskilt af komma
   uden mellemrum:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install.ps1 -Hosts STUDIO-PC,KLIPPER-PC,GRAFIK-PC
   ```

   Computerne føjes til den samme liste, som du ellers udfylder under Indstillinger ▸
   Placeringer ▸ **Tilføj computer** (se [Andre pc'er](#andre-pcer)); computere, der allerede
   står der, bliver stående. Pc'en selv springes altid over, så du kan bruge den samme kommando
   på alle pc'erne. Et ugyldigt navn stopper installationen, før noget er ændret. Uden `-Hosts`
   ændres listen ikke – du kan lige så godt tilføje computerne bagefter i Indstillinger.
3. Når der står **„Projektsøg kører – tryk Shift+Mellemrum hvor som helst"**, er du klar.

Første gang gennemgår Projektsøg alle placeringer. Projektmapperne kan findes efter få sekunder;
at læse alle filer på store netværksdelinger tager længere (en deling med 300.000 filer tager
nogle minutter). Du kan søge imens – statuslinjen øverst viser, hvad der scannes. Har du ikke
tilføjet andre computere, søger Projektsøg kun i pc'ens egne drev og diske (se
[Andre pc'er](#andre-pcer)).

**Opdatering:** Under **Indstillinger ▸ Generelt ▸ Opdatering** står, om du har den nyeste
version. Projektsøg kigger selv efter en ny version på GitHub et par gange om dagen; er der en,
kommer der en lille prik på ⚙, og knappen hedder **Opdater nu**. Et tryk henter den nye version,
kontrollerer den, udskifter filerne og genstarter Projektsøg – vinduet lukker et øjeblik, og en
besked ved uret siger, når den nye version kører. Der installeres aldrig noget, uden at du trykker.
Går noget galt undervejs, lægges de gamle filer tilbage. Er mappen hentet med `git clone`,
opdateres den med git (også GitHub Desktops git); er der ændrede filer i mappen, lægges de til
side med `git stash` først, så intet går tabt, og opdateringen kører alligevel. Kun en mappe med
sine egne commits (en udviklers) opdateres ikke automatisk.

Du kan også opdatere i hånden: kopiér de nye filer ind i mappen og kør installationen igen
(`powershell -ExecutionPolicy Bypass -File .\install.ps1`). Den stopper den kørende version og
starter den nye – den stopper kun Projektsøg selv, aldrig andre programmer. Indeks og
indstillinger bevares.

## Daglig brug

1. Tryk **Shift+Mellemrum** – uanset hvilket program du er i.
2. Skriv et par bogstaver af projektets navn. Du behøver ikke vente på vinduet: det, du taster
   lige efter genvejen, havner i søgefeltet.
3. Vælg med **↑/↓** og tryk **Enter**. Mappen åbner i Stifinder, og Projektsøg skjuler sig.

Tip til søgning:

- **Flere ord** skal alle passe: `lindholm klip` finder mappen `Klip` i projektet
  *Rikke Lindholm*.
- **Danske bogstaver er valgfrie**: `bogely` og `boegely` finder *Bøgely Jul 2024*, `forar`
  finder alt med *Forår* i navnet, og `infomøde` finder også filer, der er stavet
  *Infomoede …* (og omvendt).
- **Placeringens navn tæller med**: `forar pixelbro` finder *Pixelbro* på disken
  *Forår 2026 RØD*.
- **Filnavne** kan også søges: `FX9_7912`.
- **Tomt søgefelt** viser projektmappen for det åbne DaVinci Resolve-projekt og dine
  *Seneste projekter*.

Hvert resultat viser, hvor det ligger – f.eks. `STUDIO-PC · Kunder 2026 (STUDIO)` eller
`Disk: ARKIV (F:) · Kunder 2026 ARKIV` – samt hvornår der sidst er ændret noget,
størrelse og antal filer. Klik på en undermappe (f.eks. **Klip**) for at åbne den direkte.

Øverst kan du filtrere: **Alle · Projekter · Mapper · Filer**, **Kun online** og en bestemt
placering.

Tryk **Esc** for at rydde søgefeltet og **Esc** igen for at skjule vinduet – så kommer du
tilbage til det program, du kom fra. Inde i Projektsøg-vinduet skriver **Shift+Mellemrum** bare
et almindeligt mellemrum, så vinduet ikke forsvinder, hvis du holder Shift nede for at skrive
næste ord med stort – to hurtige tryk skjuler det. Mens vinduet er ved at komme frem, bliver
Shift+Mellemrum også til et mellemrum i søgefeltet. En genvejstast med Ctrl, Alt, Win eller en
F-tast skjuler vinduet med ét tryk. Skjuler du vinduet og kommer tilbage inden for 30
sekunder, står din søgning der stadig.

### Taster

| Tast | Handling |
|---|---|
| **Shift+Mellemrum** | Åbn Projektsøg fra ethvert program (i Projektsøg-vinduet: et almindeligt mellemrum) |
| **↑ / ↓** | Vælg resultat |
| **Enter** / dobbeltklik | Mappe: åbn i Stifinder · Fil: vis filen i sin mappe |
| **Ctrl+Enter** | Fil: åbn med standardprogrammet · Mappe: vis den i overmappen |
| **Ctrl+C** | Kopiér stien |
| **Ctrl+Shift+C** | Kopiér netværksstien (`\\PC\deling\…`) |
| **Esc** | Ryd søgefeltet – tryk igen for at skjule vinduet |
| **Ctrl+1 … Ctrl+4** | Filter: Alle, Projekter, Mapper, Filer |
| **Ctrl+,** | Indstillinger |

### Ikonet ved uret

Højreklik på Projektsøg-ikonet i meddelelsesområdet for: **Åbn Projektsøg**,
**Indstillinger …**, **Scan alle nu**, **DaVinci Resolve ▸** (Fra / Vis besked / Åbn mappe
automatisk), **Start med Windows** og **Afslut**. Et venstreklik åbner vinduet.

## Indstillinger

Åbn med ⚙ **Indstillinger** i vinduet, **Ctrl+,** eller via ikonet ved uret.

**Placeringer** – alle drev og delte mapper, sorteret efter computer:

- Hver placering har en tilstand: **Automatisk** (standard), **Medtag altid** eller
  **Medtag aldrig**. *Automatisk* medtager en placering, når der er projektmapper i den – eller,
  på bærbare diske, mediefiler (MXF, MOV, BRAW, WAV …). Begrundelsen står under placeringen,
  f.eks. „3 projektmapper fundet" eller „Ingen projektmapper fundet".
- **Scan nu** læser placeringen igen med det samme. **Glem** fjerner en offline placering fra
  indekset.
- **Tilføj mappe** medtager en bestemt mappe, som Projektsøg ikke selv har fundet (lokal sti
  eller `\\PC\deling\mappe`).
- **Computere**: de pc'er, hvis delte mapper søges igennem. Listen er tom fra start – tilføj en
  pc med dens navn under **Tilføj computer**, og fjern den med **Fjern** ved pc'ens navn (se
  [Andre pc'er](#andre-pcer)). Fjerner du en pc, glemmer Projektsøg også dens delte mapper, så de
  forsvinder fra resultaterne (du bliver spurgt først). Ligger en mappe, du selv har tilføjet
  med **Tilføj mappe**, på pc'en, skal den fjernes først. Tilføjer du pc'en igen, findes dens
  mapper igen.

**Generelt**

- **Genvejstast** (standard `shift+space`). Andre eksempler: `ctrl+shift+space`, `ctrl+f12`.
  Tilladt er `ctrl`, `alt`, `shift`, `win` plus mellemrum, et bogstav, et tal eller F1–F24.
  Undgå Ctrl+Alt-kombinationer: på et dansk tastatur er Ctrl+Alt det samme som AltGr
  (@, €, { } osv.).
- **Global genvejstast** – slå genvejen helt fra.
- **Lad DaVinci Resolve beholde Shift+Mellemrum** – se afsnittet om Resolve.
- **Udseende** – **Mørkt** (standard), **Lyst** eller **Følg Windows**.
- **Skjul efter åbning** – skjul Projektsøg, når en mappe er åbnet (standard: til).
- **Vis offline** – vis resultater fra frakoblede diske og slukkede pc'er (standard: til).
- **Start med Windows**.

**DaVinci Resolve** – slå integrationen til/fra, og vælg hvad der skal ske, når du åbner et
andet projekt i Resolve: **Fra**, **Vis besked** eller **Åbn mappe automatisk**.

**Tid** – se afsnittet om tidsregistrering. **Import** – se afsnittet om import af kort.

## Klippe – kæledyret

Slå **Klippe** til under **Indstillinger ▸ Generelt**, så bor der et lille klaptræ-kæledyr i et
smalt vindue nederst i højre side af skærm 2 (eller hovedskærmen, hvis der kun er én). Det ligger
altid øverst, og du kan trække det hen, hvor du vil – Projektsøg husker stedet. Luk det med X for
at slå det fra igen.

- **Humør:** glad og travl, mens du klipper; kigger utålmodigt rundt, når du er i et andet
  program (tiden tæller stadig); søvnig i lange pauser; sover, når Resolve er lukket.
- **Tøj efter siden:** solbriller på Color, troldmandshat på Fusion, høretelefoner på Fairlight
  og musiksider, kasket på Deliver.
- **Fester:** konfetti for hver hele time i dag, fyrværkeri når **dagens mål** er nået (standard
  6 timer), stjerner for 25/50/90 minutters fokus i træk, fest når et kort er overført eller
  tømt – og en venlig påmindelse om en pause efter 90 minutter i træk.
- **Vokser:** fra æg til baby, junior, pro og legende med al den tid, du har registreret, og viser
  🔥 dage i træk med mindst en times arbejde. Klik på den, så bliver den glad.

**Trofæer og garderobe:** Klik på 🏆 øverst i Klippe. Der er 47 trofæer – for at nå dagens mål,
holde pauser, gå hjem til tiden, arbejde stabilt (kun hverdage tæller), bruge alle Resolves sider,
overføre kort og meget mere; nogle kun i bestemte måneder, og nogle er hemmelige. Mange giver en ny
ting til garderoben: farver, striber, hatte, briller, noget i munden eller hånden og en aura. De
sjældne og legendariske ting – blandt andet seje solbriller, en cigaret og en AWP, som Klippe skyder
efter musen med, når du er væk – findes kun ved held på en arbejdsdag, og hver pc har sit eget held.

**Mad og sult:** Klippe bliver sulten, mens du arbejder (og lidt om natten, men aldrig mere end
„lidt sulten"), og så drømmer den om mad i en tankeboble, og maven knurrer. Tryk på **🍔 Mad** og
giv den en durum, en Big Mac, nuggets, pommes frites, en Faxe Kondi Booster, en Monster Mango Loco –
eller et **Booster-drop** direkte i armen. Den spiser med store bid og bøvser efter en dåse, og
energidrikkene giver den lyn og turbo et stykke tid; droppet bliver stående, til posen er tom. Er
den mæt, siger den nej tak, og efter tre energidrikke på to timer hamrer hjertet. Mad giver også
et par trofæer – og nogle af dem en ny ting i hånden.

**Beskeder:** Når en Claude-session vil bruge Resolve, er færdig eller venter på dig, kommer
beskeden op nede ved Klippe med knapperne (f.eks. **Byg nu**) og en lille lyd – den tager ikke
fokus fra Resolve, og den kommer aldrig ind i søgevinduet. Der vises én besked ad gangen; er der
flere, bladrer du med ‹ ›, og det, der venter på dit svar, kommer forrest. En session, der bare er
færdig og ikke skal have svar, kommer stille: ingen lyd, kun en lille linje „📬 1 besked · vis".
× lukker en besked. Er Klippe slået fra, viser køen sin egen Windows-besked i stedet.

**Telefonen:** Vil en session bygge („🎬 Mette vil bruge Resolve"), ringer det: Klippe får en rød
telefon, der ryster i op til 30 sekunder („📞 Mette ringer"), og der lyder en kort, lav og lidt sjov
„trrring-trrring … klap!" – én gang. Tryk på **Tag telefonen** (eller på telefonen) – Klippe svarer
„Hallo?", og så kommer spørgsmålet med **Byg nu**. Tager du den ikke, bliver det et ubesvaret opkald.
Beskeder, der bare venter på dit svar i sessionen, ringer aldrig. Vil du hellere have den almindelige
beskedlyd, så slå **Klippes egen ringelyd, når en session vil bygge** fra.

**Robotterne:** Mens en Claude-session bygger i Resolve, står Klippe med en megafon og dirigerer, og
små robotter arbejder i kassen. Rører du hverken mus eller tastatur i 3 sekunder, kommer en hel
sværm af robotter ud på skærmen ved siden af Klippe og bygger en tidslinje langs bunden: nogle bærer
klip ud af kassen, andre klipper dem over med saks. De kan ikke klikkes på, de tager aldrig fokus, og
rører du musen eller tastaturet, løber de straks ind i kassen igen (og kommer ud igen, når du holder
pause). Når sessionen er færdig, jubler de og går hjem, og Klippe fejrer det. Har Klippe AWP'en i
hånden, bliver en robot af og til uartig – så sigter Klippe og skyder den. Slå det fra med
**Robotterne må komme ud på skærmen, mens en Claude-session bygger**, eller prøv det med
**🤖 Vis robotterne** og **📞 Prøv telefonen** under 🏆.

**Renders:** Når Resolve renderer, holder Klippe øje: små robotter fodrer en render-boks, og
under Klippe står „Renderer Portræt_v3.mp4 · 47 % · ca. 3 min". Når renderen er færdig, er der
fyrværkeri og Klippes korte ringelyd – også hvis du var gået fra pc'en; fejler den, siger Klippe
hvorfor.

**Leveringsfest:** Lander en færdig render – eller en anden ny fil – i projektets **Final**-mappe,
holder Klippe leveringsfest: den grønne levérings-kasket på, filmen pakkes i en kasse med
„LEVERET ✓"-stempel, konfetti og fyrværkeri – og den roterende meme-kat kommer forbi, hopper og
tager en runde (uden lyd). Katten hentes fra GIPHY første gang; ligger der en `festkat.gif` i
Projektsøgs mappe, bruges den i stedet. Prøv det med **🎉 Prøv leveringsfesten** under 🏆, eller
slå det fra med **Leveringsfest, når en fil lander i Final**.

**Kontor-Klipper:** Får en kollegas Klippe et trofæ, eller holder den leveringsfest, kigger den kort
forbi hos dig – med sine egne farver, hat og ting i hånden – og fortæller det. Det går over
kontorets netværk (se *Filer og data*). Prøv det med **👋 Prøv et besøg**, eller slå det fra med
**Kontor-Klipper**.

Navn og dagens mål kan ændres samme sted. Klippe kører i sit eget lille vindue og tager aldrig
fokus fra Resolve.

**Klippe leger:** Når hverken mus eller tastatur er rørt i 5 minutter (kan ændres), kan Klippe
bryde ud af boksen og lege med musen på sin skærm: ride på den, flyve med den, kaste den rundt –
og så lægge den tilbage præcis, hvor den lå, og flyve hjem. Rører du musen eller tastaturet,
stopper legen med det samme, og musen er tilbage, hvor du slap den. Klippe klikker aldrig.
Babyen leger i hver pause, en junior hver anden, en pro sjældent – et æg aldrig. Der leges højst
én gang pr. pause og aldrig under afspilning i Resolve, mens et kort overføres, mens en session
bygger, i fuld skærm eller på en låst skærm (en render er fin). Slå det fra med **Klippe må lege
med musen i pauser**, eller prøv det med **Vis legen nu**.

## Import af kort

Sæt kortet fra kameraet i kortlæseren. Projektsøg kommer frem med fanen **Import** (eller viser
en besked ved uret, hvis du har slået det fra), og kortet vises også øverst i søgevinduet.

1. **Kortet**: Projektsøg kan se, hvilket kamera det er fra (FX9, FS7, A7S, DJI, GoPro – læst i
   klippenes egne filer), hvor mange klip, hvor meget det fylder, og hvornår der er optaget.
   Den ser også efter, om klippene allerede ligger i et projekt (samme navn *og* størrelse – FX9'ens
   tæller starter forfra, så navnet alene er ikke nok). Er kortet tomt – formateret i kameraet
   eller på pc'en – står der **Kortet er tomt**, så du ved, at kortet er læst uden fejl.
2. **Hvor skal klippene hen?** Øverst står forslagene: projektet, hvor nogle af kortets klip
   allerede ligger, projektet der er åbent i DaVinci Resolve, projekter du har arbejdet på i dag,
   og projekter du lige har oprettet. Du kan også søge efter et andet projekt eller vælge
   **Nyt projekt**: skriv navnet (`Kunde 2026\Projekt` lægger det i en kundemappe), og vælg en
   disk. Kun diske med skabelonen `1. KUNDENAVN` vises – med fri plads, og diske, der er for små
   til kortet, er markeret. Projektmappen laves som en kopi af skabelonen.
3. Klippene lægges løst i projektets `Klip\FX9`, `Klip\FS7`, `Klip\A7S` eller `Klip\Drone`
   (kameramappen oprettes, hvis den mangler). Ligger der allerede klip fra en anden optagedag,
   kan du vælge en ny mappe som `FX9 Dag 2`. Klip, der allerede ligger der, springes over.
4. **Kopiér og kontrollér**: Projektsøg kopierer, og bagefter læses hver kopi igen fra disken
   *og* kortets fil igen fra kortet – begge uden om Windows' hukommelse – og alle tre
   „fingeraftryk" (SHA-1) skal være ens. Så fanges også et kort eller en kortlæser, der læser
   forkert. Først når kopien er kontrolleret, får den sit rigtige navn – en afbrudt overførsel
   efterlader aldrig halve klip, og du kan bare starte igen for at fortsætte.
   Når den er færdig, kan kortet tages ud.
   **Klip (flyt fra kortet)** virker som Ctrl+X, men med kontrol: intet slettes fra kortet, før
   *alle* filer er kopieret og kontrolleret og skrevet helt ned på disken, og lige før hver fil
   slettes, tjekkes det, at kopien stadig er der med samme størrelse, og at filen på kortet er
   uændret. Går noget galt undervejs, bliver resten på kortet. Klip skal bekræftes med et ekstra
   klik. Formatér gerne kortet i kameraet før næste optagelse.
   **Kun opret mappen og åbn i Stifinder** laver mappen og åbner kortet og mappen ved siden af
   hinanden, hvis du hellere vil kopiere selv.

Sætter du et kort i, hvis filer allerede er overført (alle filer – også XML/BIM – med samme navn
og størrelse, tjekket på selve disken), siger Projektsøg **„Alle klip er overført til …"** med en
knap til mappen.

Kun **Klip** ændrer noget på kortet. Importerne huskes i
`%LOCALAPPDATA%\Projektsog\imports.json`, og hver overførsel skriver en log med navn, størrelse
og fingeraftryk for hver fil (og hvad der er slettet fra kortet) i
`%LOCALAPPDATA%\Projektsog\imports\` – også hvis pc'en går ned undervejs. Kameramodel → mappe kan
ændres i `config.json` (`import_camera_folders`, fx `"ILCE-7SM3=A7S"`).

## Tidsregistrering

Projektsøg tæller selv, hvor lang tid du bruger på hvert projekt i DaVinci Resolve – du skal
ikke starte eller stoppe noget. Uret øverst i Projektsøg viser dagens tid (rød prik = tiden
tæller lige nu); klik på det for at åbne fanen **Tid**.

**Hvad tæller med?**

- Tid, mens **DaVinci Resolve er i forgrunden** med et projekt åbent – fordelt på den side, du
  arbejder på: **Edit, Cut, Color, Fusion, Fairlight, Deliver** …
- Tid i browseren på **musik- og lydsider** som Artlist, Epidemic Sound og Musicbed – den tæller
  på det projekt, der er åbent i Resolve, som „Musik/lyd". Listen kan rettes under **Tid**.
- Tid i browseren på **AI-sider** som Higgsfield, hvor du laver AI-video og -billeder – den tæller
  på det projekt, der er åbent i Resolve, som „AI-video/billeder". Listen kan også rettes under
  **Tid**.
- **Afspilning tæller som arbejde**, også uden at du rører mus og tastatur – dog højst en time
  ad gangen, så en tidslinje, der kører i loop natten over, ikke tæller.

**Hvad tæller ikke?**

- **Pauser længere end 10 minutter** (kan ændres under **Pause efter**) – de tæller slet ikke.
  En pause er enten tid uden mus, tastatur eller afspilning, eller tid i et andet program (mail,
  Stifinder, Projektsøg …). Kortere pauser tæller med: uret kører videre, mens du er i et andet
  program, og kommer du tilbage til Resolve inden 10 minutter, tæller det hele. Ellers stopper
  tiden, fra da du forlod Resolve. Uret ved tiden viser en gul prik imens.
- Rendering alene, når ingen sidder ved maskinen, og Resolves „Untitled Project".
- Tid mens pc'en sover.

**Rapport og fakturering:** Vælg **I dag**, **Denne uge**, **Sidste måned** osv. (eller egne
datoer). Tabellen viser tiden pr. projekt og side; **Pr. dag** viser hver dag for sig, og
**Afrunding** (standard 15 min, opad) giver timerne til fakturaen. **Eksportér til Excel**
henter en CSV-fil, der åbner direkte i dansk Excel (semikolon, komma som decimaltegn). Projekter
med under 3 minutter i perioden (et projekt, der kun lige blev åbnet – også af en Claude-session)
kommer ikke med i oversigten og eksporten og bliver ikke rundet op; under tabellen står, hvor mange
der er skjult. Grænsen ændres under **Skjul korte besøg** (eller „Vis alle").

Tiden gemmes kun på den pc, hvor der er arbejdet (`%LOCALAPPDATA%\Projektsog\time.db`). Der
tages ingen skærmbilleder, og der gemmes ingen tastetryk – kun *hvornår* mus eller tastatur
sidst blev brugt, hvilket vindue der er foran, og hvad Resolve viser. Bruger I det til
medarbejdere, så fortæl dem det på forhånd.

## Bærbare diske

- Sæt disken i – Projektsøg opdager den selv inden for få sekunder. Første gang en disk ses,
  vises „Ny disk ‘…’ tilsluttet" i vinduet og som en kort besked ved uret. Er den ikke
  medtaget automatisk, kan du klikke **Medtag**.
- Diske genkendes på deres serienummer, så det gør ikke noget, at en disk får et andet
  drevbogstav næste gang.
- Når disken tages ud, bliver dens projekter stående i resultaterne, nedtonet med
  „Offline – sidst set …". Trykker du Enter på et offline resultat, står der f.eks.
  „Tilslut disken ‘2024 Disk Sølv’". Står der „Mappen findes ikke længere", er disken (eller
  pc'en) der, men mappen er flyttet, omdøbt eller slettet – glem den gamle placering under
  Indstillinger ▸ Placeringer.
- **Tip:** Giv dine diske et navn (højreklik på drevet i Stifinder ▸ *Omdøb*). Så bliver
  beskederne tydeligere end „disk uden navn (2 TB, sidst som H:)".
- **Hukommelseskort** går bare igennem: kamerakort (XDROOT, PRIVATE, DCIM) bliver aldrig
  placeringer – dem tager **Import** sig af. Andre små kort og USB-nøgler uden projektmapper
  (f.eks. en lydoptagers MUSIC-mappe) kan søges, mens de sidder i, og glemmes af sig selv et par
  minutter efter, at de er taget ud. Så hober der sig ikke en ny offline-placering op, hver gang
  et kort sættes i eller formateres. Diske med projekter, store diske og placeringer, du selv har
  valgt **Medtag altid** eller **Medtag aldrig** for, bliver stående.

## DaVinci Resolve

**Resolve-linjen** øverst i Projektsøg viser det projekt, der er åbent i Resolve, og den
projektmappe, projektets optagelser ligger i – f.eks.
„DaVinci Resolve: Rikke Lindholm - Testimonial → 📁 Rikke Lindholm (Kunder 2026 (STUDIO))".
Klik **Åbn mappe** for at åbne den, eller fold listen ud for at se alle mapper med antal klip.
Findes der ingen optagelser i en kendt projektmappe, foreslås mapper med et lignende navn som
„Muligt match". Ligger klip på en frakoblet disk, står der hvilken.

**Offline klip:** Er der røde, offline klip i Resolve, står der „⚠ 12 klip er offline i Resolve".
Tryk **Find og genlink …**: Projektsøg leder efter filerne på alle diske og computere i indekset og
viser, hvor de ligger nu („12 klip fra D:\…\Klip\FX9 → \\STUDIO-PC\…\Klip\FX9"). Er der flere
muligheder, vælger du selv. **Genlink** retter kun stierne i Resolve-projektet – ingen filer flyttes,
og projektet gemmes ikke for dig. Det er det eneste, Projektsøg nogensinde ændrer i Resolve, og
det sker aldrig, mens en Claude-session bygger, eller Resolve renderer.

Det kræver, at Resolve tillader scripting:
**DaVinci Resolve ▸ Preferences ▸ System ▸ General ▸ External scripting using = Local**
(genstart Resolve bagefter). Resolve skal have kørt i ca. 15 sekunder, før Projektsøg forbinder.

Forbindelsen til Resolve går gennem en lille hjælpeproces, som kun kører, mens Resolve er
åben. Projektsøg holder derfor ingen af Resolves filer i brug – du behøver ikke afslutte
Projektsøg, før du opdaterer DaVinci Resolve.

**Fra Resolves egen menu:** Installationen lægger scriptet *Projektsøg - Åbn projektmappe* i
**Workspace ▸ Scripts ▸ Utility**. Det åbner projektmappen for det aktuelle projekt direkte fra
Resolve – også hvis Projektsøg ikke kører. Genstart Resolve efter installationen for at se det.
(Står punktet der ikke, så se *Fejlfinding*.)

**Følg projekt** (Indstillinger ▸ DaVinci Resolve eller ikonet ved uret): når du åbner et andet
projekt i Resolve, kan Projektsøg vise en kort besked med projektmappen eller åbne mappen
automatisk i Stifinder (uden at tage fokus fra Resolve).

### Shift+Mellemrum i Resolve

Resolve bruger selv Shift+Mellemrum til at søge efter effekter. Første gang du trykker
Shift+Mellemrum, mens Resolve er aktivt, spørger Projektsøg, hvad genvejen skal gøre:

- **Åbn Projektsøg** – genvejen åbner altid Projektsøg, også i Resolve.
- **Lad Resolve beholde den** – et enkelt tryk går til Resolve som normalt; **to hurtige tryk**
  (inden for 0,4 sekund) åbner Projektsøg.

Du kan ændre valget under Indstillinger ▸ Generelt.

## Andre pc'er

- Installér Projektsøg på hver pc på samme måde (Python 3.14 skal også være installeret dér).
  Hver pc har sit eget indeks og søger både i sine egne diske og i de delte mapper på de andre
  computere.
- **Listen over computere er tom fra start** – Projektsøg søger kun i de delte mapper på de
  computere, du selv tilføjer. Skriv pc'ens navn (f.eks. `GRAFIK-PC`) under Indstillinger ▸
  Placeringer ▸ **Tilføj computer**, eller giv navnene med det samme, når du installerer:
  `-Hosts STUDIO-PC,KLIPPER-PC,GRAFIK-PC` (se [Installation](#installation)). Navnet er det,
  kommandoen `hostname` viser i PowerShell på den pc; en IP-adresse virker også. Derefter finder
  Projektsøg selv pc'ens delte mapper.
- En pc springer altid sig selv over, så alle pc'erne kan have den samme liste.
- De delte mapper skal kunne åbnes af brugeren – prøv at skrive `\\GRAFIK-PC` i Stifinders
  adresselinje. Administrative delinger som `D$` bruges ikke.
- Er en pc slukket, bliver dens projekter stående som offline med beskeden
  „Computeren GRAFIK-PC svarer ikke – er den tændt?". Er pc'en taget ud af brug, så fjern den
  med **Fjern** ved pc'ens navn under Indstillinger ▸ Placeringer – så forsvinder dens mapper
  også fra resultaterne.
- Netværksscanning er skånsom: den kører i baggrunden med lav prioritet, én stor scanning ad
  gangen pr. pc, og derefter læses kun ændringer.

## Fejlfinding

**Shift+Mellemrum gør ingenting**
- Er Projektsøg-ikonet ved uret? Ellers start Projektsøg fra Start-menuen.
- Er **Global genvejstast** slået til under Indstillinger ▸ Generelt?
- I selve Projektsøg-vinduet skriver Shift+Mellemrum et mellemrum – brug **Esc** for at skjule
  vinduet.
- Genvejen reagerer bevidst ikke, hvis du tastede noget andet lige før (under 0,3 sekund) – så
  den ikke springer frem, mens du skriver.
- Et andet program kan have taget samme genvej – vælg en anden under Indstillinger ▸ Generelt.
  Så vises også beskeden „Genvejstasten … kunne ikke aktiveres".
- I programmer, der kører som administrator, kan Windows ikke lade Projektsøg se tastetryk.
  Brug ikonet ved uret.

**Vinduet kommer ikke frem** – Microsoft Edge skal være installeret. Lige efter login kan der gå
et par sekunder. Prøv Start-menuen ▸ Projektsøg.

**Installationen siger, at Projektsøg er startet som administrator** – så kan et almindeligt
PowerShell-vindue ikke stoppe den (det sker, hvis install.ps1 engang blev kørt som
administrator). Afslut Projektsøg fra ikonet ved uret (højreklik ▸ Afslut) – eller log af og
på igen – og kør installationen (eller afinstallationen) igen.

**En mappe mangler i resultaterne**
- Kører den første scanning stadig? Se statuslinjen øverst.
- Ligger mappen på en anden pc, skal pc'en være tilføjet under Indstillinger ▸ Placeringer ▸
  **Tilføj computer** (se [Andre pc'er](#andre-pcer)).
- Under Indstillinger ▸ Placeringer: står placeringen som *Ikke medtaget*? Vælg
  **Medtag altid**.
- Er **Kun online** slået til, eller er der valgt et filter?
- Klik **Scan nu** på placeringen. Nye projektmapper dukker normalt op af sig selv, få sekunder
  efter du har åbnet vinduet.

**„Placeringen svarer ikke"** – disken eller pc'en svarede ikke inden for 3 sekunder (en disk
der skal vågne, et langsomt netværk). Prøv igen om lidt.

**„Findes ikke længere – indekset opdateres"** – mappen er flyttet eller slettet. Indekset
retter sig selv; søg igen om et øjeblik.

**Resolve-linjen viser en fejl** – tjek *External scripting using = Local* (se ovenfor) og
genstart Resolve.

**Projektsøg står ikke under Workspace ▸ Scripts ▸ Utility i Resolve** – genstart Resolve.
Står punktet der stadig ikke, så kopiér filen `Projektsoeg - Aabn projektmappe.py` fra
programmappens `resolve_scripts` til
`%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility` og genstart Resolve
igen. Det er samme script, bare uden æ, ø og å i navnet (til Resolve-versioner, der ikke viser
dem). Afinstallationen fjerner begge.

**Projektsøg starter ikke** – se logfilerne (nedenfor), især `startup-error.log`. Du kan også
starte fra PowerShell i programmappen og få detaljer direkte i vinduet:
`python -m projektsog --debug` (kører Projektsøg allerede, så afslut den først – ellers bringer
kommandoen bare det kørende vindue frem).

**Start forfra med indekset** – Åbn PowerShell i programmappen og kør
`pythonw -m projektsog --rescan`: så læses alle placeringer helt forfra. Kører Projektsøg
allerede, er det den kørende Projektsøg, der scanner (vinduet kommer frem, og statuslinjen
viser scanningen). Vil du slette indekset helt, så afslut Projektsøg (ikonet ved uret ▸ Afslut),
slet filerne `index.db*` i `%LOCALAPPDATA%\Projektsog`, og start Projektsøg igen. **Bemærk:**
Dine valg af *Medtag altid* og *Medtag aldrig* for de enkelte placeringer gemmes også i
indekset og nulstilles derfor, og diske, Projektsøg har set før, kan blive meldt som nye igen.

**Logfiler** ligger i `%LOCALAPPDATA%\Projektsog\logs` (skriv stien i Stifinders
adresselinje):

| Fil | Indhold |
|---|---|
| `projektsog.log` | Programmets log (højst 4 × 2 MB) |
| `scanworker.log` | Scanning af diske og delte mapper |
| `crash.log` | Tekniske detaljer, hvis Python selv går ned |
| `startup-error.log` | Fejl, der forhindrede Projektsøg i at starte |

## Filer og data

Alt, hvad Projektsøg gemmer, ligger i `%LOCALAPPDATA%\Projektsog`:

| Fil/mappe | Indhold |
|---|---|
| `config.json` | Indstillingerne |
| `index.db` | Søgeindekset og dine valg af *Medtag altid/aldrig* pr. placering (kan slettes – indekset bygges op igen, men valgene nulstilles) |
| `instance.json` | Hvilken port den kørende Projektsøg bruger (til Resolve-scriptet) |
| `logs\` | Logfiler |
| `edge-profile\` | Den private Edge-profil til Projektsøgs vindue |

Projektsøg lytter kun på `127.0.0.1` (port 47811 eller den næste ledige) – den kan ikke nås
fra andre computere. Den eneste undtagelse er **Kontor-Klipper**: Klippe sender og modtager små
hilsner (UDP port 47850) med de andre pc'er på kontoret – kun navn, udseende og trofæ, aldrig
filer eller kommandoer. Første gang spørger Windows måske, om Projektsøg må bruge netværket; sig
ja (eller slå **Kontor-Klipper** fra, så lyttes der ikke).

## Kommandolinje (avanceret)

```text
python -m projektsog [--background] [--no-window] [--port N] [--debug] [--rescan]
```

| Parameter | Betydning |
|---|---|
| `--background` | Start skjult; vinduet forberedes i baggrunden (bruges af *Start med Windows*) |
| `--no-window` | Kun indeks og lokal server – uden vindue, ikon og genvejstast (til test) |
| `--port N` | Første port der prøves (standard 47811) |
| `--debug` | Detaljeret log – også i konsollen, når der er en |
| `--rescan` | Læs alle placeringer helt forfra |

Starter du Projektsøg, mens den allerede kører, bringes det kørende vindue frem, og `--rescan`
sendes videre til den kørende Projektsøg. De øvrige parametre gælder kun, når Projektsøg ikke
kører i forvejen. Starter du Projektsøg, lige mens den er ved at lukke (f.eks. lige efter
**Afslut**), venter den nye start, til den gamle er lukket, og tager så over.

## Afinstallation

```powershell
powershell -ExecutionPolicy Bypass -File .\uninstall.ps1
```

Stopper Projektsøg (kun Projektsøg selv – aldrig andre programmer) og fjerner genvejen,
*Start med Windows* og Resolve-scriptet. Indeks og indstillinger bevares; tilføj `-RemoveData`
for også at slette dem. Til sidst kan programmappen slettes.

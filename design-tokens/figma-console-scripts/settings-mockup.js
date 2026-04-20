// ============================================
// Krab Ear Settings Panel Mockup — paste in Figma Console
// Instructions:
// 1. Open https://www.figma.com/design/IPngmhIJEH93vCoeliJkuV
// 2. Menu → Plugins → Development → Open Console
// 3. Paste this code → press Run
// ============================================

(async () => {
  // --- Layout constants ---
  const FRAME_W      = 900;
  const FRAME_H      = 700;
  const PADDING      = 24;
  const SECTION_GAP  = 12;
  const SECTION_R    = 12;
  const HEADER_H     = 44;
  const BODY_H       = 120;

  // Section definitions (5 Settings sections)
  const SECTIONS = [
    { title: 'Запись / STT',    icon: '🎙' },
    { title: 'Перевод',         icon: '🌐' },
    { title: 'LLM',             icon: '🤖' },
    { title: 'Горячие клавиши', icon: '⌨️' },
    { title: 'Интерфейс',       icon: '🎨' },
  ];

  // --- Load local variables to bind if available ---
  const allVars = await figma.variables.getLocalVariablesAsync();
  const allCollections = await figma.variables.getLocalVariableCollectionsAsync();

  function findVar(name) {
    return allVars.find(v => v.name === name || v.name.endsWith('/' + name));
  }

  // --- Helper: create styled rectangle ---
  function makeRect(name, x, y, w, h, fills) {
    const r = figma.createRectangle();
    r.name = name;
    r.x = x; r.y = y; r.w = w; r.h = h;
    r.fills = fills;
    return r;
  }

  // --- Helper: create text node ---
  async function makeText(content, x, y, fontSize, color, bold) {
    await figma.loadFontAsync({ family: 'Inter', style: bold ? 'Semi Bold' : 'Regular' });
    const t = figma.createText();
    t.characters = content;
    t.x = x; t.y = y;
    t.fontSize = fontSize;
    t.fills = [{ type: 'SOLID', color }];
    return t;
  }

  // --- Color helpers ---
  const bgColor      = { r: 0.11, g: 0.11, b: 0.14 };   // ~#1C1C24 dark bg
  const sectionBg    = { r: 0.16, g: 0.16, b: 0.20 };   // ~#29293A section card
  const headerBg     = { r: 0.20, g: 0.20, b: 0.26 };   // slightly lighter header
  const textPrimary  = { r: 1,    g: 1,    b: 1    };
  const textSecondary= { r: 0.55, g: 0.55, b: 0.65 };
  const accentColor  = { r: 0.47, g: 0.70, b: 1.00 };   // ~#78B2FF

  // --- Create root frame ---
  const frame = figma.createFrame();
  frame.name = 'Settings Panel — Krab Ear';
  frame.resize(FRAME_W, FRAME_H);
  frame.fills = [{ type: 'SOLID', color: bgColor }];
  frame.cornerRadius = 16;
  frame.clipsContent = true;

  // Bind bg color if Elevation/card variable exists
  const cardColorVar = findVar('cardShadowColor');
  // (shadow variables can't bind directly to fills, so we skip binding here)

  // --- Title bar ---
  const titleBar = makeRect('TitleBar', 0, 0, FRAME_W, 56,
    [{ type: 'SOLID', color: headerBg }]);
  frame.appendChild(titleBar);

  const titleText = await makeText('Настройки — Krab Ear', PADDING, 16, 16, textPrimary, true);
  frame.appendChild(titleText);

  // --- Sections ---
  let currentY = 56 + PADDING;

  for (let i = 0; i < SECTIONS.length; i++) {
    const sec = SECTIONS[i];
    const sectionX = PADDING;
    const sectionW = FRAME_W - PADDING * 2;

    // Section card background
    const card = makeRect(`Section/${sec.title}/bg`, sectionX, currentY, sectionW, HEADER_H + BODY_H,
      [{ type: 'SOLID', color: sectionBg }]);
    card.cornerRadius = SECTION_R;
    frame.appendChild(card);

    // Header row background
    const headerRect = makeRect(`Section/${sec.title}/header`, sectionX, currentY, sectionW, HEADER_H,
      [{ type: 'SOLID', color: headerBg }]);
    headerRect.cornerRadius = SECTION_R;
    // Only round top corners
    headerRect.topLeftRadius   = SECTION_R;
    headerRect.topRightRadius  = SECTION_R;
    headerRect.bottomLeftRadius  = 0;
    headerRect.bottomRightRadius = 0;
    frame.appendChild(headerRect);

    // Disclosure triangle
    const arrow = await makeText('▶', sectionX + 12, currentY + 14, 12, textSecondary, false);
    frame.appendChild(arrow);

    // Icon + title
    const titleNode = await makeText(
      `${sec.icon}  ${sec.title}`,
      sectionX + 32, currentY + 12, 14, textPrimary, true
    );
    frame.appendChild(titleNode);

    // Body placeholder rows
    const rowLabels = ['Параметр 1', 'Параметр 2', 'Параметр 3'];
    for (let r = 0; r < rowLabels.length; r++) {
      const rowY = currentY + HEADER_H + 12 + r * 32;

      const lbl = await makeText(rowLabels[r], sectionX + 16, rowY, 12, textSecondary, false);
      frame.appendChild(lbl);

      // Value chip
      const chip = makeRect(`Chip/${sec.title}/${r}`, sectionX + sectionW - 120, rowY - 4, 104, 24,
        [{ type: 'SOLID', color: { r: 0.25, g: 0.25, b: 0.32 } }]);
      chip.cornerRadius = 6;
      frame.appendChild(chip);

      const chipText = await makeText('значение', sectionX + sectionW - 108, rowY, 11, accentColor, false);
      frame.appendChild(chipText);
    }

    // Separator line between sections (except last)
    if (i < SECTIONS.length - 1) {
      const sep = makeRect(`Sep/${i}`, sectionX, currentY + HEADER_H + BODY_H + 1, sectionW, 1,
        [{ type: 'SOLID', color: { r: 0.22, g: 0.22, b: 0.28 } }]);
      frame.appendChild(sep);
    }

    currentY += HEADER_H + BODY_H + SECTION_GAP;
  }

  // --- Position frame in viewport ---
  const vp = figma.viewport.center;
  frame.x = vp.x - FRAME_W / 2;
  frame.y = vp.y - FRAME_H / 2;

  figma.viewport.scrollAndZoomIntoView([frame]);
  figma.currentPage.selection = [frame];

  console.log(`Done: created Settings Panel frame (${FRAME_W}×${FRAME_H}) with ${SECTIONS.length} sections`);
})();

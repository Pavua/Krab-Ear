// ============================================
// Krab Ear Elevation Collection — paste in Figma Console
// Instructions:
// 1. Open https://www.figma.com/design/IPngmhIJEH93vCoeliJkuV
// 2. Menu → Plugins → Development → Open Console
// 3. Paste this code → press Run
// ============================================

(async () => {
  // --- Token values from krab-ear-tokens.json ---
  const tokens = {
    card: {
      shadowColor:    { r: 0, g: 0, b: 0, a: 1 },
      shadowOpacity:  0.15,
      shadowBlur:     6,
      shadowOffsetX:  0,
      shadowOffsetY:  -2,
      shadowSpread:   0,
    },
    popup: {
      shadowColor:    { r: 0, g: 0, b: 0, a: 1 },
      shadowOpacity:  0.20,
      shadowBlur:     16,
      shadowOffsetX:  0,
      shadowOffsetY:  -6,
      shadowSpread:   0,
    },
    overlay: {
      shadowColor:    { r: 0, g: 0, b: 0, a: 1 },
      shadowOpacity:  0.30,
      shadowBlur:     32,
      shadowOffsetX:  0,
      shadowOffsetY:  -12,
      shadowSpread:   0,
    },
  };

  // Variable types available in plugin API
  const STRING  = 'STRING';
  const FLOAT   = 'FLOAT';
  const COLOR   = 'COLOR';

  // --- Create or reuse collection ---
  const existingCollections = await figma.variables.getLocalVariableCollectionsAsync();
  let collection = existingCollections.find(c => c.name === 'Elevation');
  if (!collection) {
    collection = figma.variables.createVariableCollection('Elevation');
    collection.renameMode(collection.modes[0].modeId, 'Default');
  }
  const modeId = collection.modes[0].modeId;

  // Helper: create or update a variable
  async function upsertVar(name, type, value) {
    const existing = (await figma.variables.getLocalVariablesAsync())
      .find(v => v.variableCollectionId === collection.id && v.name === name);
    const variable = existing ?? figma.variables.createVariable(name, collection, type);
    variable.setValueForMode(modeId, value);
    return variable;
  }

  let count = 0;

  for (const [group, vals] of Object.entries(tokens)) {
    await upsertVar(`${group}/shadowColor`,   COLOR,  vals.shadowColor);   count++;
    await upsertVar(`${group}/shadowOpacity`, FLOAT,  vals.shadowOpacity); count++;
    await upsertVar(`${group}/shadowBlur`,    FLOAT,  vals.shadowBlur);    count++;
    await upsertVar(`${group}/shadowOffsetX`, FLOAT,  vals.shadowOffsetX); count++;
    await upsertVar(`${group}/shadowOffsetY`, FLOAT,  vals.shadowOffsetY); count++;
    await upsertVar(`${group}/shadowSpread`,  FLOAT,  vals.shadowSpread);  count++;
  }

  console.log(`Done: created/updated ${count} variables in "Elevation" collection`);
})();

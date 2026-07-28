export function getDefaultRenameName(folderName) {
  const safeName = String(folderName || '').trim();
  if (!safeName) return '';
  const [prefix] = safeName.split('_');
  return prefix || safeName;
}

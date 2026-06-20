const TEXT_SEARCH_FIELD_TYPES = new Set(['string', 'string[]']);

export function getSearchableFields(fields = []) {
  return fields
    .filter((field) => TEXT_SEARCH_FIELD_TYPES.has(field.type))
    .map((field) => field.name)
    .filter(Boolean);
}

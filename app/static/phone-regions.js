/* Region filtering is local: no company addresses are sent to a geocoder.
 * Province boundaries and telephone areas differ. Known addresses take priority.
 * Sources and the complete CBS 2026 place-name lookup are in phone-regions.json.
 */
const PHONE_NORTH_PROVINCES = ['Groningen', 'Drenthe', 'Friesland'];
const phonePlaceIndexes = new WeakMap();
function normalizedPlace(value) {
  return String(value || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '')
    .toLowerCase().replace(/[’‘]/g, "'").replace(/\s+/g, ' ').trim();
}
function companyProvince(location, data) {
  if (!location) return null;
  let text = normalizedPlace(location).replace(/[,.\s]+$/, '')
    .replace(/(?:[,\s]+|\()(?:nederland|netherlands|nl)\)?$/, '').trim();
  const provinces = ['Groningen','Drenthe','Friesland','Overijssel','Flevoland',
    'Gelderland','Utrecht','Noord-Holland','Zuid-Holland','Zeeland','Noord-Brabant','Limburg'];
  const explicit = provinces.find(province => text === normalizedPlace(province) ||
    text.endsWith(', '+normalizedPlace(province)) || text.endsWith('('+normalizedPlace(province)+')'));
  if (explicit) return explicit;
  if (/(?:^|[,\s(])fryslan\)?$/.test(text)) return 'Friesland';
  let places = phonePlaceIndexes.get(data);
  if (!places) {
    places = new Map();
    for (const [name, regions] of Object.entries(data.places)) {
      const key = normalizedPlace(name);
      // BAG sometimes distinguishes duplicate names with a suffix (e.g.
      // Oosterwolde Gld). The unsuffixed name remains ambiguous.
      for (const alias of new Set([key,key.replace(/ (?:gld|gn|fr|dr|zh|nh|nb|ut|ov|zld|lb|l)$/, '')]))
        places.set(alias, [...new Set([...(places.get(alias)||[]), ...regions])]);
    }
    phonePlaceIndexes.set(data, places);
  }
  // Only match the address's final locality, never a company name or street.
  const postal = text.match(/\b\d{4}\s?[a-z]{2}\s+(.+)$/);
  if (postal) text = postal[1];
  else {
    text = text.split(',').pop().trim();
    const houseNumber = text.match(/\b\d+[a-z]?\s+([^\d]+)$/);
    if (houseNumber) text = houseNumber[1];
  }
  const words = text.split(' ');
  for (let offset = 0; offset < words.length; offset++) {
    const candidate = words.slice(offset).join(' ');
    const regions = places.get(candidate);
    // Duplicate place names across provinces cannot establish an address.
    if (regions) return regions.length === 1 ? regions[0] : null;
    // Without a postcode, require the place to follow a house number. This
    // prevents a street such as "Groningen" being treated as a town.
    if (!postal && !/^\d+[a-z]?$/.test(words[offset])) break;
  }
  return null;
}
function phoneRegion(phone, data) {
  const province = phone.company ? companyProvince(phone.location, data) : null;
  if (province) return {provinces:[province], basis:'company', label:province+' · bedrijfslocatie'};
  const prefix = Object.keys(data.area_provinces).sort((a,b)=>b.length-a.length)
    .find(code => String(phone.number||'').startsWith(code));
  if (!prefix) return {provinces:[], basis:'unknown', label:''};
  const provinces = data.area_provinces[prefix];
  return {provinces, basis:'area', label:prefix+' · '+provinces.join(' / ')+
    (provinces.length > 1 ? ' · gedeeld netnummergebied' : ' · netnummergebied')};
}
function matchesPhoneRegion(phone, selected, data) {
  if (!selected) return true;
  const provinces = phoneRegion(phone, data).provinces;
  return selected === 'north' ? provinces.some(p=>PHONE_NORTH_PROVINCES.includes(p)) : provinces.includes(selected);
}
if (typeof module !== 'undefined') module.exports = {companyProvince, phoneRegion, matchesPhoneRegion};

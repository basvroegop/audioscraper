const {test} = require('node:test');
const assert = require('node:assert/strict');
const {companyProvince,phoneRegion,matchesPhoneRegion} = require('../app/static/phone-regions.js');
const data = require('../app/static/phone-regions.json');

test('service and mobile business numbers use their address',()=>{
  for(const number of ['08001220','0881234567','0612345678']) {
    const phone={number,company:'Bedrijf',location:'De Werf 1, Heerenveen'};
    assert.equal(matchesPhoneRegion(phone,'north',data),true);
    assert.equal(matchesPhoneRegion(phone,'Friesland',data),true);
    assert.equal(matchesPhoneRegion(phone,'Groningen',data),false);
    assert.equal(phoneRegion(phone,data).basis,'company');
  }
});
test('known company outside the north overrides a shared area code',()=>{
  const phone={number:'0523648635',company:'Restaurant',location:'Stelling 2, 7773 ND Hardenberg'};
  assert.equal(matchesPhoneRegion(phone,'north',data),false);
  assert.equal(matchesPhoneRegion(phone,'',data),true);
  assert.equal(companyProvince('Gramsbergen',data),'Overijssel');
});
test('unknown geographic numbers match provinces and disclose shared areas',()=>{
  for(const number of ['0501234567','0592123456','0581234567','0512123456','0521123456','0594123456'])
    assert.equal(matchesPhoneRegion({number},'north',data),true);
  assert.equal(matchesPhoneRegion({number:'0570633637'},'north',data),false);
  assert.equal(matchesPhoneRegion({number:'0612345678'},'north',data),false);
  assert.equal(matchesPhoneRegion({number:'0881234567'},'north',data),false);
  assert.match(phoneRegion({number:'0521123456'},data).label,/gedeeld netnummergebied/);
});
test('CBS place names, diacritics, address formats and ambiguous names',()=>{
  assert.equal(companyProvince('Tingietersstraat 14, 8601 WJ Sneek',data),'Friesland');
  assert.equal(companyProvince('Hoofdstraat 2, 9401 AA Assen, Nederland',data),'Drenthe');
  assert.equal(companyProvince('De Werf 1 Heerenveen',data),'Friesland');
  assert.equal(companyProvince('Groningenstraat 1, Amsterdam',data),'Noord-Holland');
  assert.equal(companyProvince('Groningenstraat 1',data),null);
  assert.equal(companyProvince('Friesland',data),'Friesland');
  assert.equal(companyProvince('Fryslân',data),'Friesland');
  assert.equal(companyProvince('Oosterwolde',data),null);
  assert.equal(companyProvince('Hoofdstraat 1, Oosterwolde (Friesland)',data),'Friesland');
  assert.ok(Object.keys(data.places).length>2400);
});

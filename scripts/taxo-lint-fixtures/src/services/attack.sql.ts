export const q1 = `taxo.master query type = 'Line1_Bad' more`;
export const q2 = `SELECT FROM taxo.master WHERE a = ${cond ? `type = 'Nested_Ignored'` : ''} AND type = 'Real_Bad'`;
export const q3 = `SELECT FROM taxo.master WHERE n = ${escape("it's a test")} AND type = 'Quote_Bad'`;

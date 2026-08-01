it('queries taxo.master correctly', () => {
  const type = 'Fixture_Type';
  expect(sqlFor('taxo.master')).toContain('type');
});

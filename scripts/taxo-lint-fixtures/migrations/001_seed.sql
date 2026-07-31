INSERT INTO x SELECT value FROM taxo.master
  WHERE type = 'Org_Department' AND hierarchy_level = 'leaf';

local chunk, err = loadfile(arg[1])
if not chunk then print('PARSE_FAIL|' .. tostring(err)) os.exit(1) end
print('PARSE_OK')

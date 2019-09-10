import vltpf.SPHERE as SPHERE

ds = SPHERE.Dataset('/Users/avigan/data/VLTPF-test-target/test/', log_level='debug')

print('IRDIS reductions:')
for red in ds.IRDIS_reductions:
    print(red)
print()

print('IFS reductions:')
for red in ds.IFS_reductions:
    print(red)
print()

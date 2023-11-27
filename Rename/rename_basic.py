import os

# editable
target_dir = './data'

# editable
def get_name(index: int):
    return '2023_%04d'%(index)

def solve(preview = True):
    files=os.listdir(target_dir)
    files.sort()

    for i in range(len(files)):
        file = files[i]
        type_name=file.split('.')[-1]
        target_name = get_name(i) + '.' + type_name

        if preview:
            print(file, '>>>', target_name)
        else:
            os.rename(os.path.join(target_dir,file),os.path.join(target_dir,target_name))

def main():
    solve()

    yes = input('OK? (Y/N)')[0]
    if yes == 'Y' or yes == 'y':
        solve(False)
        print('done.')
    else:
        print('bye.')

if __name__=='__main__':
    main()
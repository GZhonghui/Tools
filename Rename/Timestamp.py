# Save the Log
# python Timestamp.py > Log.txt

import os,time
supportType=['png','jpg']

def main(): 
    files=os.listdir('.') 

    # This Helps
    # files.sort(key=os.path.getctime)

    for file in files: 
        typeName=file.split('.')[-1] 
        if typeName not in supportType: 
            print('%48s | Not support'%file) 
            continue 

        createTime=os.path.getmtime(file) 
        createTime=time.localtime(createTime) 

        targetName='Name_%04d%02d%02d%02d%02d%02d'%( \
            createTime.tm_year,createTime.tm_mon,createTime.tm_mday, \
            createTime.tm_hour,createTime.tm_min,createTime.tm_sec \
            )+'.'+typeName 

        print('%48s | %s'%(file,targetName)) 
        os.rename(file,targetName) 

if __name__=='__main__':  
    main()
#include"message.h"

std::unordered_set<unsigned int> printed;

void Message::print(MessageType message_type,const char *message)
{
    time_t now_time=time(nullptr);
    tm *local_time=localtime(&now_time);
    local_time->tm_mon+=1;

    switch(message_type)
    {
        case MessageType::MESSAGE:
        {
            printf("\033[32m[MESSAGE]\033[0m");
        }
        break;

        case MessageType::WARNING:
        {
            printf("\033[33m[WARNING]\033[0m");
        }
        break;

        case MessageType::ERROR:
        {
            printf("\033[31m[ ERROR ]\033[0m");
        }
        break;
    }

    printf(" \033[34m[%04d-%02d-%02d %02d:%02d:%02d]\033[0m",
        1900+local_time->tm_year,local_time->tm_mon,local_time->tm_mday,
        local_time->tm_hour,local_time->tm_min,local_time->tm_sec);

    printf(" %s\n",message);
    fflush(stdout);
}

void Message::print_once(unsigned int id,const char *message)
{
    if(!printed.count(id))
    {
        printed.insert(id);

        time_t now_time=time(nullptr);
        tm *local_time=localtime(&now_time);
        local_time->tm_mon+=1;

        printf("\033[33m[ DEBUG ]\033[0m");
        printf(" \033[34m[%04d-%02d-%02d %02d:%02d:%02d]\033[0m",
            1900+local_time->tm_year,local_time->tm_mon,local_time->tm_mday,
            local_time->tm_hour,local_time->tm_min,local_time->tm_sec);

        printf(" %s\n",message);
        fflush(stdout);
    }
}

void Message::print_bar(int now,bool last,int width)
{
    now=std::min(100,std::max(0,now));
    width=std::min(100,std::max(20,width));

    char message[message_size];
    int real_width=width-12;
    int blocks=std::max(1,(int)(now*width/100.0));
    for(int i=0;i<=width;i+=1)
    {
        message[i]=(i==width?0:(i==blocks-1?'>':(i>=blocks?' ':'=')));
    }

    printf("\r\033[32m[ #%03d%% ]\033[0m [%s]",now,message);
    if(last) puts("");
    fflush(stdout);
}

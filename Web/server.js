const express = require('express');
const path = require('path');
const app = express();
const port = 3000;

// 设置静态文件目录
app.use(express.static('public'));

// 设置视图引擎为 EJS
app.set('view engine', 'ejs');
app.set('views', path.join(__dirname, 'views'));

// 路由设置
app.get('/', (req, res) => {
    res.render('index', {
        title: '工具集合',
        tools: [
            { 
                id: 'json-formatter', 
                name: 'JSON格式化工具', 
                description: '一个简单的JSON格式化和验证工具',
                url: '/tools/json-formatter'
            },
            {
                id: 'file-checksum',
                name: '文件校验和计算器',
                description: '计算文件的MD5、SHA-1、SHA-256等哈希值',
                url: '/tools/file-checksum'
            },
            {
                id: 'batch-rename',
                name: '文件批量重命名',
                description: '生成批量重命名命令，支持模式串重命名',
                url: '/tools/batch-rename'
            }
        ]
    });
});

// 工具路由
app.get('/tools/:toolId', (req, res) => {
    res.render(`tools/${req.params.toolId}`);
});

app.listen(port, () => {
    console.log(`server is running on http://localhost:${port}`);
});

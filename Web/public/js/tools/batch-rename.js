window.onload = function() {
    const toolContent = document.getElementById('batch-rename-content');
    
    toolContent.innerHTML = `
        <div class="row">
            <div class="col-12">
                <div class="form-group mb-3">
                    <label class="form-label">选择文件</label>
                    <div class="drop-zone p-5 border rounded text-center" id="dropZone">
                        <div class="mb-3">
                            <input type="file" id="fileInput" webkitdirectory directory multiple class="d-none">
                            <button class="btn btn-outline-primary me-2" onclick="document.getElementById('fileInput').click()">
                                选择目录
                            </button>
                            或将文件夹拖放到这里
                        </div>
                        <div id="fileList" class="text-start" style="display: none;">
                            <h6>选中的文件：</h6>
                            <div class="small" style="max-height: 200px; overflow-y: auto;"></div>
                        </div>
                    </div>
                </div>
                <div class="form-group mb-3">
                    <label for="filePattern" class="form-label">文件匹配模式（可选）</label>
                    <input type="text" class="form-control" id="filePattern" placeholder="例如: *.jpg 或 IMG_*.JPG">
                    <small class="form-text text-muted">留空处理目录下所有文件，或输入通配符模式匹配特定文件</small>
                </div>
                <div class="form-group mb-3">
                    <label for="pattern" class="form-label">重命名模式</label>
                    <div class="input-group">
                        <input type="text" class="form-control" id="pattern" placeholder="例如: photo_{id}.{ext}">
                        <button class="btn btn-outline-secondary" type="button" data-bs-toggle="popover" 
                            data-bs-trigger="focus" title="模式说明" 
                            data-bs-content="{id}: 序号&#10;{name}: 原文件名&#10;{ext}: 扩展名">
                            ?
                        </button>
                    </div>
                    <small class="form-text text-muted">
                        支持的变量：{id}序号, {name}原文件名, {ext}扩展名
                    </small>
                </div>
                <div class="form-group mb-3">
                    <label for="startNum" class="form-label">起始序号</label>
                    <input type="number" class="form-control" id="startNum" value="1">
                </div>
                <div class="form-group mb-3">
                    <label for="numDigits" class="form-label">序号位数（补零）</label>
                    <input type="number" class="form-control" id="numDigits" value="2">
                </div>
                <div class="form-group mb-3">
                    <div class="form-check">
                        <input class="form-check-input" type="checkbox" id="sortByName" checked>
                        <label class="form-check-label" for="sortByName">
                            按文件名排序
                        </label>
                    </div>
                    <div class="form-check">
                        <input class="form-check-input" type="checkbox" id="previewMode" checked>
                        <label class="form-check-label" for="previewMode">
                            预览模式（生成的命令会先显示将要执行的操作，而不是直接执行）
                        </label>
                    </div>
                </div>
                <button class="btn btn-primary" id="generateBtn">生成重命名脚本</button>
            </div>
            <div class="col-12 mt-4">
                <div class="card">
                    <div class="card-header d-flex justify-content-between align-items-center">
                        <span>脚本预览</span>
                        <div>
                            <button class="btn btn-sm btn-outline-secondary me-2" id="previewBtn" style="display: none;">
                                预览文件列表
                            </button>
                            <button class="btn btn-sm btn-primary" id="downloadBtn" style="display: none;">
                                下载脚本
                            </button>
                        </div>
                    </div>
                    <div class="card-body">
                        <div id="previewArea" style="display: none;" class="mb-3">
                            <pre class="mb-0"><code id="previewOutput"></code></pre>
                        </div>
                        <pre class="mb-0"><code id="commandOutput">输入目录路径和重命名模式后生成脚本</code></pre>
                    </div>
                </div>
            </div>
        </div>
    `;

    // 初始化所有 popovers
    var popoverTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="popover"]'));
    var popoverList = popoverTriggerList.map(function (popoverTriggerEl) {
        return new bootstrap.Popover(popoverTriggerEl, {html: true});
    });

    const dropZone = document.getElementById('dropZone');
    const fileInput = document.getElementById('fileInput');
    const fileList = document.getElementById('fileList');
    const fileListContent = fileList.querySelector('div');
    let selectedFiles = [];

    // 处理拖放
    dropZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropZone.classList.add('border-primary');
    });

    dropZone.addEventListener('dragleave', (e) => {
        e.preventDefault();
        dropZone.classList.remove('border-primary');
    });

    dropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropZone.classList.remove('border-primary');
        
        // 获取所有文件
        const items = e.dataTransfer.items;
        handleItems(items);
    });

    // 处理文件选择
    fileInput.addEventListener('change', (e) => {
        selectedFiles = Array.from(e.target.files);
        updateFileList();
    });

    // 递归处理文件和目录
    async function handleItems(items) {
        selectedFiles = [];
        for (let item of items) {
            if (item.kind === 'file') {
                const entry = item.webkitGetAsEntry();
                if (entry) {
                    await traverseEntry(entry);
                }
            }
        }
        updateFileList();
    }

    // 递归遍历目录
    async function traverseEntry(entry) {
        if (entry.isFile) {
            const file = await getFileFromEntry(entry);
            selectedFiles.push(file);
        } else if (entry.isDirectory) {
            const reader = entry.createReader();
            const entries = await readEntriesPromise(reader);
            for (let entry of entries) {
                await traverseEntry(entry);
            }
        }
    }

    // 将 FileEntry 转换为 File
    function getFileFromEntry(entry) {
        return new Promise((resolve) => {
            entry.file(resolve);
        });
    }

    // 读取目录内容
    function readEntriesPromise(reader) {
        return new Promise((resolve) => {
            reader.readEntries((entries) => {
                resolve(entries);
            });
        });
    }

    // 更新文件列表显示
    function updateFileList() {
        if (selectedFiles.length > 0) {
            fileList.style.display = 'block';
            fileListContent.innerHTML = selectedFiles
                .map(file => `<div>${file.webkitRelativePath || file.name}</div>`)
                .join('');
        } else {
            fileList.style.display = 'none';
        }
    }

    // 修改生成脚本函数
    function generateScript() {
        if (selectedFiles.length === 0) {
            alert('请选择文件');
            return;
        }

        if (!pattern.value) {
            alert('请输入重命名模式');
            return;
        }

        // 按文件名排序（如果需要）
        if (sortByName.checked) {
            selectedFiles.sort((a, b) => (a.webkitRelativePath || a.name).localeCompare(b.webkitRelativePath || b.name));
        }

        // 生成脚本内容
        const script = `#!/bin/bash

# 显示将要处理的文件数量
echo "找到 ${selectedFiles.length} 个文件"
echo "----------------------------"

# 重命名文件
count=${startNum.value}
`;

        // 为每个文件生成重命名命令
        const commands = selectedFiles.map(file => {
            const filePath = file.webkitRelativePath || file.name;
            const ext = filePath.split('.').pop();
            const nameWithoutExt = filePath.substring(0, filePath.lastIndexOf('.'));
            
            let newName = pattern.value
                .replace(/{id}/g, String(parseInt(startNum.value)).padStart(parseInt(numDigits.value), '0'))
                .replace(/{name}/g, nameWithoutExt)
                .replace(/{ext}/g, ext);

            if (!newName.includes('.') && ext) {
                newName += '.' + ext;
            }

            return `
# 重命名：${filePath}
if ${previewMode.checked}; then
    echo "将重命名："
    echo "  从：${filePath}"
    echo "  到：${newName}"
    echo "----------------------------"
else
    mv "${filePath}" "${newName}"
    echo "已重命名：${filePath} -> ${newName}"
fi
count=$((count + 1))
`;
        }).join('\n');

        const finalScript = script + commands + `
if ${previewMode.checked}; then
    echo "预览模式：以上是将要执行的重命名操作"
    echo "要执行实际重命名，请将脚本中的 previewMode 设置为 false 后重新运行"
fi
`;

        commandOutput.textContent = finalScript;
        downloadBtn.style.display = 'inline-block';
        return finalScript;
    }

    // 下载脚本文件
    downloadBtn.addEventListener('click', function() {
        const script = generateScript();
        const blob = new Blob([script], { type: 'text/plain' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
        a.href = url;
        a.download = `rename_files_${timestamp}.sh`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    });

    // 预览文件列表
    previewBtn.addEventListener('click', function() {
        const findCmd = `find "${dirPath.value}" -type f${filePattern.value ? ` -name "${filePattern.value}"` : ''}${sortByName.checked ? ' | sort' : ''}`;
        previewOutput.textContent = `将处理以下文件：\n${findCmd}\n\n运行上述命令可查看具体文件列表`;
        previewArea.style.display = 'block';
    });

    generateBtn.addEventListener('click', generateScript);

    // 当任何输入改变时启用生成按钮
    [dirPath, filePattern, pattern, startNum, numDigits, sortByName, previewMode].forEach(input => {
        input.addEventListener('change', () => {
            generateBtn.disabled = false;
        });
    });
}; 
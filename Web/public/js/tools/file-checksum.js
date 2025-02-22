window.onload = function() {
    const toolContent = document.getElementById('file-checksum-content');
    
    // 创建工具界面
    toolContent.innerHTML = `
        <div class="row">
            <div class="col-12">
                <div class="form-group mb-3">
                    <label for="fileInput" class="form-label">选择文件</label>
                    <input type="file" class="form-control" id="fileInput">
                </div>
                <div class="progress mb-2" style="display: none;">
                    <div class="progress-bar" role="progressbar" style="width: 0%">
                        <span class="progress-text">读取文件...</span>
                    </div>
                </div>
                <div class="status-text mb-3" style="display: none;">
                    <small class="text-muted">正在计算哈希值，请稍候...</small>
                </div>
                <div class="card mb-3">
                    <div class="card-body">
                        <h5 class="card-title">计算结果</h5>
                        <div class="row">
                            <div class="col-md-4">
                                <div class="mb-3">
                                    <label class="form-label">MD5</label>
                                    <div class="input-group">
                                        <input type="text" class="form-control" id="md5Result" readonly>
                                        <button class="btn btn-outline-secondary copy-btn" type="button" data-result="md5Result">复制</button>
                                    </div>
                                </div>
                            </div>
                            <div class="col-md-4">
                                <div class="mb-3">
                                    <label class="form-label">SHA-1</label>
                                    <div class="input-group">
                                        <input type="text" class="form-control" id="sha1Result" readonly>
                                        <button class="btn btn-outline-secondary copy-btn" type="button" data-result="sha1Result">复制</button>
                                    </div>
                                </div>
                            </div>
                            <div class="col-md-4">
                                <div class="mb-3">
                                    <label class="form-label">SHA-256</label>
                                    <div class="input-group">
                                        <input type="text" class="form-control" id="sha256Result" readonly>
                                        <button class="btn btn-outline-secondary copy-btn" type="button" data-result="sha256Result">复制</button>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    `;

    const fileInput = document.getElementById('fileInput');
    const md5Result = document.getElementById('md5Result');
    const sha1Result = document.getElementById('sha1Result');
    const sha256Result = document.getElementById('sha256Result');
    const progressBar = document.querySelector('.progress');
    const progressBarInner = document.querySelector('.progress-bar');
    const progressText = document.querySelector('.progress-text');
    const statusText = document.querySelector('.status-text');

    // 复制按钮功能
    document.querySelectorAll('.copy-btn').forEach(btn => {
        btn.addEventListener('click', function() {
            const resultId = this.getAttribute('data-result');
            const resultInput = document.getElementById(resultId);
            resultInput.select();
            document.execCommand('copy');
            
            // 显示复制成功提示
            const originalText = this.textContent;
            this.textContent = '已复制';
            setTimeout(() => {
                this.textContent = originalText;
            }, 1000);
        });
    });

    // 创建 Web Worker
    const workerCode = `
        importScripts('https://cdnjs.cloudflare.com/ajax/libs/crypto-js/4.1.1/crypto-js.min.js');
        
        let md5Hash = CryptoJS.algo.MD5.create();
        let sha1Hash = CryptoJS.algo.SHA1.create();
        let sha256Hash = CryptoJS.algo.SHA256.create();
        
        self.onmessage = function(e) {
            const { data, isLast } = e.data;
            
            // 更新所有哈希值
            const wordArray = CryptoJS.lib.WordArray.create(data);
            md5Hash.update(wordArray);
            sha1Hash.update(wordArray);
            sha256Hash.update(wordArray);
            
            if (isLast) {
                // 完成计算，返回最终结果
                self.postMessage({
                    md5: md5Hash.finalize().toString(),
                    sha1: sha1Hash.finalize().toString(),
                    sha256: sha256Hash.finalize().toString()
                });
            } else {
                // 通知进度更新
                self.postMessage({ progress: true });
            }
        };
    `;

    fileInput.addEventListener('change', async function(e) {
        const file = e.target.files[0];
        if (!file) return;

        // 重置显示
        md5Result.value = '';
        sha1Result.value = '';
        sha256Result.value = '';
        progressBar.style.display = 'flex';
        statusText.style.display = 'block';
        progressBarInner.style.width = '0%';
        progressText.textContent = '处理文件...';

        try {
            const worker = new Worker(URL.createObjectURL(new Blob([workerCode], { type: 'application/javascript' })));
            const CHUNK_SIZE = 1024 * 1024 * 10; // 10MB 块大小
            const totalChunks = Math.ceil(file.size / CHUNK_SIZE);
            let processedChunks = 0;

            worker.onmessage = function(e) {
                if (e.data.progress) {
                    // 更新进度
                    processedChunks++;
                    const progress = Math.round((processedChunks / totalChunks) * 100);
                    progressBarInner.style.width = `${progress}%`;
                    progressText.textContent = `处理进度: ${progress}%`;
                } else {
                    // 显示最终结果
                    const results = e.data;
                    md5Result.value = results.md5;
                    sha1Result.value = results.sha1;
                    sha256Result.value = results.sha256;

                    progressBarInner.style.width = '100%';
                    progressText.textContent = '完成';
                    statusText.style.display = 'none';
                    
                    setTimeout(() => {
                        progressBar.style.display = 'none';
                    }, 1000);

                    worker.terminate();
                }
            };

            // 分块读取和处理文件
            for (let start = 0; start < file.size; start += CHUNK_SIZE) {
                const chunk = file.slice(start, start + CHUNK_SIZE);
                const arrayBuffer = await chunk.arrayBuffer();
                const isLast = start + CHUNK_SIZE >= file.size;
                
                worker.postMessage({
                    data: arrayBuffer,
                    isLast: isLast
                });
            }

        } catch (error) {
            progressBar.style.display = 'none';
            statusText.style.display = 'none';
            alert('处理文件时发生错误：' + error.message);
        }
    });
}; 
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[Windows.Forms.Application]::EnableVisualStyles()
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$form = New-Object Windows.Forms.Form
$form.Text = 'Video Localizer · Seedance 配置'
$form.StartPosition = 'CenterScreen'
$form.ClientSize = New-Object Drawing.Size(570,574)
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
function Add-Field($title, $value, $top, $secret) {
    $label = New-Object Windows.Forms.Label
    $label.Text = $title
    $label.SetBounds(16,$top,538,20)
    $form.Controls.Add($label)
    $box = New-Object Windows.Forms.TextBox
    $box.Text = $value
    $box.UseSystemPasswordChar = $secret
    $box.SetBounds(16,($top+22),538,26)
    $form.Controls.Add($box)
    return $box
}
$endpoint = Add-Field 'Seedance / Ark API 地址' $env:VL_DIALOG_API 14 $false
$model = Add-Field 'Seedance 模型 ID / 推理接入点 ID（ep-...）' $env:VL_DIALOG_MODEL 77 $false
$familyLabel = New-Object Windows.Forms.Label
$familyLabel.Text = 'Seedance 系列（接入点 ID 必须明确选择）'
$familyLabel.SetBounds(16,140,538,20)
$form.Controls.Add($familyLabel)
$family = New-Object Windows.Forms.ComboBox
$family.DropDownStyle = 'DropDownList'
$family.SetBounds(16,162,538,26)
[void]$family.Items.Add('seedance-2.0')
[void]$family.Items.Add('seedance-2.5')
$form.Controls.Add($family)
$key = Add-Field 'Ark API Key（遮挡输入；留空沿用该 API 地址的已保存凭据）' '' 203 $true
$mediaEndpoint = Add-Field '自有素材服务器（https://你的域名/codex-media/v1）' $env:VL_DIALOG_MEDIA_API 266 $false
$mediaKey = Add-Field '独立上传 Key（禁止使用 Ark Key；留空沿用该服务器的凭据）' '' 329 $true
$uploadStatus = New-Object Windows.Forms.Label
$uploadStatus.SetBounds(16,395,538,42)
$form.Controls.Add($uploadStatus)
$message = New-Object Windows.Forms.Label
$message.SetBounds(16,450,538,62)
$form.Controls.Add($message)
$script:updatingFamily = $false
$script:currentModelId = $model.Text.Trim()
function Set-FamilyForModel($fallback) {
    $script:updatingFamily = $true
    try {
        $family.SelectedIndex = -1
        $family.SelectedItem = $null
        if ($model.Text.Trim() -ceq 'doubao-seedance-2-0-260128') {
            $family.SelectedItem = 'seedance-2.0'
        } elseif ($model.Text.Trim() -ceq 'doubao-seedance-2-5-260628') {
            $family.SelectedItem = 'seedance-2.5'
        } elseif ($fallback -in @('seedance-2.0','seedance-2.5')) {
            $family.SelectedItem = $fallback
        }
    } finally {
        $script:updatingFamily = $false
    }
}
function Update-UploadStatus {
    if ([string]::IsNullOrWhiteSpace($mediaEndpoint.Text)) {
        $uploadStatus.ForeColor = [Drawing.Color]::Firebrick
        $uploadStatus.Text = '自有素材上传服务尚未配置：视频流程需要独立服务器地址和上传 Key。'
    } else {
        $uploadStatus.ForeColor = [Drawing.SystemColors]::ControlText
        $uploadStatus.Text = '素材上传至以上自有服务器，再由 Seedance 读取。两个接口分别使用各自凭据。'
    }
}
function Update-FamilyForModelChange {
    $nextModelId = $model.Text.Trim()
    if ($nextModelId -ceq $script:currentModelId) { return }
    $script:currentModelId = $nextModelId
    # An edited deployment ID must not inherit another deployment's family.
    Set-FamilyForModel ''
    if ($null -eq $family.SelectedItem) {
        $message.Text = '模型或接入点 ID 已变更，请重新选择 Seedance 系列后保存。'
    } else {
        $message.Text = '配置只保存在本机；保存不会测试连接或自动生成视频。'
    }
}
Set-FamilyForModel $env:VL_DIALOG_FAMILY
Update-UploadStatus
$message.Text = '配置只保存在本机；保存不会测试连接或自动生成视频。'
$model.Add_TextChanged({ Update-FamilyForModelChange })
$mediaEndpoint.Add_TextChanged({ Update-UploadStatus })
$family.Add_SelectedIndexChanged({
    if (-not $script:updatingFamily -and $model.Text.Trim() -cin @('doubao-seedance-2-0-260128','doubao-seedance-2-5-260628')) {
        if ($family.SelectedItem -eq 'seedance-2.0') { $model.Text = 'doubao-seedance-2-0-260128' }
        elseif ($family.SelectedItem -eq 'seedance-2.5') { $model.Text = 'doubao-seedance-2-5-260628' }
    }
})
$ok = New-Object Windows.Forms.Button
$ok.Text = '保存'
$ok.SetBounds(458,526,96,32)
$ok.Add_Click({
    if ($form.Tag -eq 'saved') { $form.Close(); return }
    $ok.Enabled = $false
    try {
        $fields = @{provider='seedance'; base_url=$endpoint.Text; model=$model.Text; model_family=[string]$family.SelectedItem; key=$key.Text}
        # Empty initial media fields permit model setup only. The result clearly
        # reports missing upload setup; configured fields are loaded on opening.
        if (-not [string]::IsNullOrWhiteSpace($mediaEndpoint.Text) -or -not [string]::IsNullOrWhiteSpace($mediaKey.Text)) {
            $fields.media_endpoint = $mediaEndpoint.Text
            $fields.media_key = $mediaKey.Text
        }
        $request = $fields | ConvertTo-Json -Compress
        [Console]::WriteLine($request)
        [Console]::Out.Flush()
        $response = [Console]::ReadLine()
        if ($null -eq $response) { throw 'Connection closed' }
        $result = ConvertFrom-Json -InputObject $response
        $message.Text = $result.message
        if ($result.ok) {
            $form.Tag = 'saved'
            $key.Clear()
            $mediaKey.Clear()
            $endpoint.ReadOnly = $model.ReadOnly = $key.ReadOnly = $mediaEndpoint.ReadOnly = $mediaKey.ReadOnly = $true
            $family.Enabled = $false
            $message.ForeColor = [Drawing.Color]::DarkGreen
            $ok.Text = '完成'
        } else {
            $message.ForeColor = [Drawing.Color]::Firebrick
        }
    } catch {
        $message.ForeColor = [Drawing.Color]::Firebrick
        $message.Text = '配置程序连接已断开。请重新打开窗口后再保存。'
    } finally {
        $ok.Enabled = $true
    }
})
$form.AcceptButton = $ok
$form.Controls.Add($ok)
[void]$form.ShowDialog()
$saved = $form.Tag -eq 'saved'
$form.Dispose()
if (-not $saved) { exit 2 }

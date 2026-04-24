[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$AccountId,

    [string]$Region = "us-east-1",
    [string]$LocalImage = "cost-optimization-worker:local",
    [string]$EcrRepository = "cost-optimization-worker",
    [string]$ImageTag = (Get-Date -Format "yyyyMMdd-HHmmss"),

    [Parameter(Mandatory = $true)]
    [string]$TaskDefinitionFamily,

    [switch]$UploadEnv,
    [string]$EnvFilePath = "config/cost_optimization/cost-optimization.worker.env",
    [string]$EnvS3Uri = "",

    [switch]$UpdateScheduler,
    [string]$ScheduleName = "",
    [string]$ScheduleGroup = "default"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Assert-Tool {
    param([Parameter(Mandatory = $true)][string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Missing required tool: $Name"
    }
}

function Assert-ExitCode {
    param([Parameter(Mandatory = $true)][string]$Step)
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE"
    }
}

function Invoke-AwsJson {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $output = aws @Arguments
    Assert-ExitCode -Step ("aws " + ($Arguments -join " "))
    if ([string]::IsNullOrWhiteSpace($output)) {
        return $null
    }
    return $output | ConvertFrom-Json
}

function Get-ObjectPropertyValueOrNull {
    param(
        [Parameter(Mandatory = $true)]
        [object]$InputObject,
        [Parameter(Mandatory = $true)]
        [string]$PropertyName
    )

    if ($null -eq $InputObject) {
        return $null
    }

    $property = $InputObject.PSObject.Properties[$PropertyName]
    if ($null -eq $property) {
        return $null
    }

    return $property.Value
}

Assert-Tool -Name "aws"
Assert-Tool -Name "docker"

if ($UploadEnv -and [string]::IsNullOrWhiteSpace($EnvS3Uri)) {
    throw "When -UploadEnv is set, -EnvS3Uri is required."
}

if ($UpdateScheduler -and [string]::IsNullOrWhiteSpace($ScheduleName)) {
    throw "When -UpdateScheduler is set, -ScheduleName is required."
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$registry = "$AccountId.dkr.ecr.$Region.amazonaws.com"
$ecrImage = "$registry/${EcrRepository}:$ImageTag"

Write-Host "==> Using repository root: $repoRoot"
Write-Host "==> Target ECR image: $ecrImage"

Push-Location $repoRoot
try {
    Write-Host "==> Building Docker image: $LocalImage"
    docker build -t $LocalImage .
    Assert-ExitCode -Step "docker build"

    Write-Host "==> Logging in to ECR registry: $registry"
    $password = aws ecr get-login-password --region $Region
    Assert-ExitCode -Step "aws ecr get-login-password"
    $password | docker login --username AWS --password-stdin $registry
    Assert-ExitCode -Step "docker login"

    Write-Host "==> Tagging image: $LocalImage -> $ecrImage"
    docker tag $LocalImage $ecrImage
    Assert-ExitCode -Step "docker tag"

    Write-Host "==> Pushing image to ECR"
    docker push $ecrImage
    Assert-ExitCode -Step "docker push"

    if ($UploadEnv) {
        Write-Host "==> Uploading env file to S3: $EnvFilePath -> $EnvS3Uri"
        aws s3 cp $EnvFilePath $EnvS3Uri --region $Region --sse AES256
        Assert-ExitCode -Step "aws s3 cp"
    }

    Write-Host "==> Fetching current ECS task definition family: $TaskDefinitionFamily"
    $taskDefResponse = Invoke-AwsJson -Arguments @(
        "ecs", "describe-task-definition",
        "--task-definition", $TaskDefinitionFamily,
        "--region", $Region
    )

    $taskDef = $taskDefResponse.taskDefinition
    $containerDefinitions = @($taskDef.containerDefinitions)

    if ($containerDefinitions.Count -eq 0) {
        throw "No container definitions found in task definition family: $TaskDefinitionFamily"
    }

    $containerToUpdate = $containerDefinitions[0]
    Write-Host "==> Updating container image for: $($containerToUpdate.name)"
    $containerToUpdate.image = $ecrImage

    $registerPayload = [ordered]@{
        family = $taskDef.family
        containerDefinitions = $containerDefinitions
    }

    $optionalProperties = @(
        "taskRoleArn",
        "executionRoleArn",
        "networkMode",
        "volumes",
        "placementConstraints",
        "requiresCompatibilities",
        "cpu",
        "memory",
        "runtimePlatform",
        "proxyConfiguration",
        "inferenceAccelerators",
        "ephemeralStorage",
        "pidMode",
        "ipcMode"
    )

    # These ECS fields must always be JSON arrays; ConvertFrom-Json unwraps single-element arrays into plain strings.
    $alwaysArrayProperties = @("requiresCompatibilities", "volumes", "placementConstraints", "inferenceAccelerators")

    foreach ($prop in $optionalProperties) {
        $value = Get-ObjectPropertyValueOrNull -InputObject $taskDef -PropertyName $prop
        if ($null -eq $value) {
            continue
        }
        if ($value -is [string] -and [string]::IsNullOrWhiteSpace($value)) {
            continue
        }
        if ($prop -in $alwaysArrayProperties -and $value -isnot [array]) {
            $value = @($value)
        }
        $registerPayload[$prop] = $value
    }

    if ($taskDefResponse.tags) {
        $registerPayload["tags"] = $taskDefResponse.tags
    }

    $payloadPath = Join-Path $env:TEMP ("ecs-register-taskdef-" + [Guid]::NewGuid().ToString() + ".json")
    try {
        $json = $registerPayload | ConvertTo-Json -Depth 100
        [System.IO.File]::WriteAllText($payloadPath, $json, (New-Object System.Text.UTF8Encoding $false))

        Write-Host "==> Registering new ECS task definition revision"
        $fileUri = "file://" + $payloadPath.Replace('\', '/')
        $registerResponse = Invoke-AwsJson -Arguments @(
            "ecs", "register-task-definition",
            "--region", $Region,
            "--cli-input-json", $fileUri
        )
    }
    finally {
        if (Test-Path $payloadPath) {
            Remove-Item -Path $payloadPath -Force
        }
    }

    $newTaskDefArn = $registerResponse.taskDefinition.taskDefinitionArn
    Write-Host "==> New task definition revision: $newTaskDefArn"

    if ($UpdateScheduler) {
        Write-Host "==> Updating EventBridge Scheduler target: $ScheduleGroup/$ScheduleName"

        $schedule = Invoke-AwsJson -Arguments @(
            "scheduler", "get-schedule",
            "--name", $ScheduleName,
            "--group-name", $ScheduleGroup,
            "--region", $Region
        )

        if ($null -eq $schedule.target -or $null -eq $schedule.target.ecsParameters) {
            throw "Schedule target is not ECS RunTask or does not contain ecsParameters."
        }

        $schedule.target.ecsParameters.taskDefinitionArn = $newTaskDefArn

        $flexibleWindowJson = $schedule.flexibleTimeWindow | ConvertTo-Json -Compress -Depth 20
        $targetJson = $schedule.target | ConvertTo-Json -Compress -Depth 100

        $updateArgs = @(
            "scheduler", "update-schedule",
            "--name", $ScheduleName,
            "--group-name", $ScheduleGroup,
            "--region", $Region,
            "--schedule-expression", $schedule.scheduleExpression,
            "--flexible-time-window", $flexibleWindowJson,
            "--target", $targetJson,
            "--state", $schedule.state
        )

        $scheduleExpressionTimezone = Get-ObjectPropertyValueOrNull -InputObject $schedule -PropertyName "scheduleExpressionTimezone"
        if ($scheduleExpressionTimezone) {
            $updateArgs += @("--schedule-expression-timezone", $scheduleExpressionTimezone)
        }
        $startDate = Get-ObjectPropertyValueOrNull -InputObject $schedule -PropertyName "startDate"
        if ($startDate) {
            $updateArgs += @("--start-date", $startDate)
        }
        $endDate = Get-ObjectPropertyValueOrNull -InputObject $schedule -PropertyName "endDate"
        if ($endDate) {
            $updateArgs += @("--end-date", $endDate)
        }
        $description = Get-ObjectPropertyValueOrNull -InputObject $schedule -PropertyName "description"
        if ($description) {
            $updateArgs += @("--description", $description)
        }
        $kmsKeyArn = Get-ObjectPropertyValueOrNull -InputObject $schedule -PropertyName "kmsKeyArn"
        if ($kmsKeyArn) {
            $updateArgs += @("--kms-key-arn", $kmsKeyArn)
        }
        $actionAfterCompletion = Get-ObjectPropertyValueOrNull -InputObject $schedule -PropertyName "actionAfterCompletion"
        if ($actionAfterCompletion) {
            $updateArgs += @("--action-after-completion", $actionAfterCompletion)
        }

        aws @updateArgs | Out-Null
        Assert-ExitCode -Step "aws scheduler update-schedule"

        Write-Host "==> Scheduler updated to new task definition revision"
    }

    Write-Host ""
    Write-Host "Done."
    Write-Host "Image pushed: $ecrImage"
    Write-Host "New task definition: $newTaskDefArn"
    if (-not $UpdateScheduler) {
        Write-Host "Scheduler not updated (run again with -UpdateScheduler if needed)."
    }
}
finally {
    Pop-Location
}

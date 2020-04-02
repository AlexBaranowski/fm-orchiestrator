{% include "snippets/c3i-library.groovy" %}
import static org.apache.commons.lang.StringEscapeUtils.escapeHtml;
pipeline {
  {% include "snippets/default-agent.groovy" %}
  options {
    timestamps()
    timeout(time: 120, unit: 'MINUTES')
    buildDiscarder(logRotator(numToKeepStr: '10'))
    skipDefaultCheckout()
  }
  environment {
    TRIGGER_NAMESPACE = readFile("/run/secrets/kubernetes.io/serviceaccount/namespace").trim()
    PAGURE_API = "${params.PAGURE_URL}/api/0"
    PAGURE_REPO_IS_FORK = "${params.PAGURE_REPO_IS_FORK}"
    PAGURE_REPO_HOME = "${env.PAGURE_URL}${env.PAGURE_REPO_IS_FORK == 'true' ? '/fork' : ''}/${params.PAGURE_REPO_NAME}"
  }
  stages {
    stage('Prepare') {
      steps {
        script {
          c3i.clone(repo: params.MBS_GIT_REPO, branch: params.MBS_GIT_REF)
          // get current commit ID
          // FIXME: Due to a bug discribed in https://issues.jenkins-ci.org/browse/JENKINS-45489,
          // the return value of checkout() is unreliable.
          // Not working: env.MBS_GIT_COMMIT = scmVars.GIT_COMMIT
          env.MBS_GIT_COMMIT = sh(returnStdout: true, script: 'git rev-parse HEAD').trim()
          // Set for pagure function from c3i-library
          env.GIT_COMMIT = env.MBS_GIT_COMMIT
          echo "Build ${params.MBS_GIT_REF}, commit=${env.MBS_GIT_COMMIT}"

          env.IMAGE_IS_SCRATCH = (params.MBS_GIT_REF != params.MBS_MAIN_BRANCH)
          // Is the current branch a pull-request? If no, env.PR_NO will be empty.
          env.PR_NO = getPrNo(params.MBS_GIT_REF)

          // Generate a version-release number for the target Git commit
          env.MBS_VERSION = sh(script: """grep -m 1 -P -o '(?<=version=")[^"]+' setup.py""", returnStdout: true).trim()
          env.BUILD_SUFFIX = ".jenkins${currentBuild.id}.git${env.MBS_GIT_COMMIT.take(7)}"
          env.TEMP_TAG = "${env.MBS_VERSION}${env.BUILD_SUFFIX}"

          def resp = httpRequest params.MBS_SPEC_FILE
          env.SPEC_FILE_NAME = params.MBS_SPEC_FILE.split("/").last()
          writeFile file: env.SPEC_FILE_NAME, text: resp.content
          sh """
            sed -i \
                -e 's/Version:.*/Version:        ${env.MBS_VERSION}/' \
                -e 's/%{?dist}/${env.BUILD_SUFFIX}%{?dist}/' \
                ${env.SPEC_FILE_NAME}

          """
          sh 'mkdir repos'
          params.EXTRA_REPOS.split().each {
            resp = httpRequest it
            writeFile file: "repos/${it.split("/").last()}", text: resp.content
          }
          sh """
            sed -i \
                -e '/enum34/d' \
                -e '/funcsigs/d' \
                -e '/futures/d' \
                -e '/koji/d' \
                requirements.txt
          """
          sh """
            sed -i \
                -e 's/py.test/py.test-3/g' \
                -e '/basepython/d' \
                -e '/sitepackages/a setenv = PYTHONPATH={toxinidir}' \
                tox.ini
          """
          {% include "snippets/get_paas_domain.groovy" %}
        }
      }
    }
    stage('Update Build Info') {
      when {
        expression {
          return params.PAGURE_URL && params.PAGURE_REPO_NAME
        }
      }
      steps {
        script {
          // Set friendly display name and description
          if (env.PR_NO) { // is pull-request
            env.PR_URL = "${env.PAGURE_REPO_HOME}/pull-request/${env.PR_NO}"
            echo "Building PR #${env.PR_NO}: ${env.PR_URL}"
            // NOTE: Old versions of OpenShift Client Jenkins plugin are buggy to handle arguments
            // with special bash characters (like whitespaces, #, etc).
            // https://bugzilla.redhat.com/show_bug.cgi?id=1625518
            currentBuild.displayName = "PR#${env.PR_NO}"
            // To enable HTML syntax in build description, go to `Jenkins/Global Security/Markup Formatter` and select 'Safe HTML'.
            def pagureLink = """<a href="${env.PR_URL}">${currentBuild.displayName}</a>"""
            try {
              def prInfo = pagure.getPR(env.PR_NO)
              pagureLink = """<a href="${env.PR_URL}">PR#${env.PR_NO}: ${escapeHtml(prInfo.title)}</a>"""
              // set PR status to Pending
              if (params.PAGURE_API_KEY_SECRET_NAME)
                pagure.setBuildStatusOnPR(null, "Build #${env.BUILD_NUMBER} in progress (commit: ${env.MBS_GIT_COMMIT.take(8)})")
            } catch (Exception e) {
              echo "Error using pagure API: ${e}"
            }
            currentBuild.description = pagureLink
          } else { // is a branch
            currentBuild.displayName = "${env.MBS_GIT_REF}: ${env.MBS_GIT_COMMIT.take(7)}"
            currentBuild.description = """<a href="${env.PAGURE_REPO_HOME}/c/${env.MBS_GIT_COMMIT}">${currentBuild.displayName}</a>"""
            if (params.PAGURE_API_KEY_SECRET_NAME) {
              try {
                pagure.flagCommit('pending', null, "Build #${env.BUILD_NUMBER} in progress (commit: ${env.MBS_GIT_COMMIT.take(8)})")
                echo "Updated commit ${env.MBS_GIT_COMMIT} status to PENDING."
              } catch (e) {
		echo "Error updating commit ${env.MBS_GIT_COMMIT} status to PENDING: ${e}"
              }
            }
          }
	}
      }
    }
    stage('Allocate C3IaaS project') {
      when {
        expression {
          return params.USE_C3IAAS == 'true'
        }
      }
      steps {
        script {
          if (!params.C3IAAS_REQUEST_PROJECT_BUILD_CONFIG_NAME ||
              !params.C3IAAS_REQUEST_PROJECT_BUILD_CONFIG_NAMESPACE) {
            error("USE_C3IAAS is set to true but missing C3IAAS_REQUEST_PROJECT_BUILD_CONFIG_NAME" +
                  " or C3IAAS_REQUEST_PROJECT_BUILD_CONFIG_NAMESPACE")
          }
          if (env.PR_NO) {
            env.PIPELINE_ID = "c3i-mbs-pr-${env.PR_NO}-git${env.MBS_GIT_COMMIT.take(8)}"
          } else {
            env.PIPELINE_ID = "c3i-mbs-${params.MBS_GIT_REF}-git${env.MBS_GIT_COMMIT.take(8)}"
          }
          echo "Requesting new OpenShift project ${env.PIPELINE_ID}..."
          openshift.withCluster() {
            openshift.withProject(params.C3IAAS_REQUEST_PROJECT_BUILD_CONFIG_NAMESPACE) {
              c3i.buildAndWait(script: this, objs: "bc/${params.C3IAAS_REQUEST_PROJECT_BUILD_CONFIG_NAME}",
                '-e', "PROJECT_NAME=${env.PIPELINE_ID}",
                '-e', "ADMIN_GROUPS=system:serviceaccounts:${TRIGGER_NAMESPACE},system:serviceaccounts:${PIPELINE_AS_A_SERVICE_BUILD_NAMESPACE}",
                '-e', "LIFETIME_IN_MINUTES=${params.C3IAAS_LIFETIME}"
              )
            }
          }
        }
      }
      post {
        success {
          echo "Allocated project ${env.PIPELINE_ID}"
        }
        failure {
          echo "Failed to allocate ${env.PIPELINE_ID} project"
        }
      }
    }
    stage('Build backend image') {
      environment {
        BACKEND_BUILDCONFIG_ID = "mbs-backend-build-${currentBuild.id}-${UUID.randomUUID().toString().take(7)}"
      }
      steps {
        script {
          openshift.withCluster() {
            openshift.withProject(env.PIPELINE_ID) {
              // OpenShift BuildConfig doesn't support specifying a tag name at build time.
              // We have to create a new BuildConfig for each image build.
              echo 'Creating a BuildConfig for mbs-backend build...'
              def created = new Date().format("yyyy-MM-dd'T'HH:mm:ss'Z'", TimeZone.getTimeZone('UTC'))
              def template = readYaml file: 'openshift/backend/mbs-backend-build-template.yaml'
              def processed = openshift.process(template,
                '-p', "NAME=${env.BACKEND_BUILDCONFIG_ID}",
                '-p', "MBS_GIT_REPO=${params.MBS_GIT_REPO}",
                // A pull-request branch, like pull/123/head, cannot be built with commit ID
                // because refspec cannot be customized in an OpenShift build.
                '-p', "MBS_GIT_REF=${env.PR_NO ? params.MBS_GIT_REF : env.MBS_GIT_COMMIT}",
                '-p', "MBS_BACKEND_IMAGESTREAM_NAME=${params.MBS_BACKEND_IMAGESTREAM_NAME}",
                '-p', "MBS_BACKEND_IMAGESTREAM_NAMESPACE=${env.PIPELINE_ID}",
                '-p', "MBS_IMAGE_TAG=${env.TEMP_TAG}",
                '-p', "EXTRA_RPMS=${params.EXTRA_RPMS}",
                '-p', "CREATED=${created}"
              )
              def build = c3i.buildAndWait(script: this, objs: processed, '--from-dir=.')
              def ocpBuild = build.object()
              env.BACKEND_IMAGE_DIGEST = ocpBuild.status.output.to.imageDigest
              def ref = ocpBuild.status.outputDockerImageReference
              def repo = ref.tokenize(':')[0..-2].join(':')
              env.BACKEND_IMAGE_REPO = repo
              env.BACKEND_IMAGE_REF = repo + '@' + env.BACKEND_IMAGE_DIGEST
              env.BACKEND_IMAGE_TAG = env.TEMP_TAG
              echo "Built image ${env.BACKEND_IMAGE_REF}, digest: ${env.BACKEND_IMAGE_DIGEST}, tag: ${env.BACKEND_IMAGE_TAG}"
            }
          }
        }
      }
      post {
        failure {
          echo "Failed to build mbs-backend image ${env.TEMP_TAG}."
        }
        cleanup {
          script {
            if (params.USE_C3IAAS != 'true') {
              openshift.withCluster() {
                openshift.withProject(env.PIPELINE_ID) {
                  echo 'Tearing down...'
                  openshift.selector('bc', [
                    'app': env.BACKEND_BUILDCONFIG_ID,
                    'template': 'mbs-backend-build-template',
                    ]).delete()
                }
              }
            }
          }
        }
      }
    }
    stage('Build frontend image') {
      environment {
        FRONTEND_BUILDCONFIG_ID = "mbs-frontend-build-${currentBuild.id}-${UUID.randomUUID().toString().take(7)}"
      }
      steps {
        script {
          openshift.withCluster() {
            openshift.withProject(env.PIPELINE_ID) {
              // OpenShift BuildConfig doesn't support specifying a tag name at build time.
              // We have to create a new BuildConfig for each image build.
              echo 'Creating a BuildConfig for mbs-frontend build...'
              def created = new Date().format("yyyy-MM-dd'T'HH:mm:ss'Z'", TimeZone.getTimeZone('UTC'))
              def template = readYaml file: 'openshift/frontend/mbs-frontend-build-template.yaml'
              def processed = openshift.process(template,
                '-p', "NAME=${env.FRONTEND_BUILDCONFIG_ID}",
                '-p', "MBS_GIT_REPO=${params.MBS_GIT_REPO}",
                // A pull-request branch, like pull/123/head, cannot be built with commit ID
                // because refspec cannot be customized in an OpenShift build.
                '-p', "MBS_GIT_REF=${env.PR_NO ? params.MBS_GIT_REF : env.MBS_GIT_COMMIT}",
                '-p', "MBS_FRONTEND_IMAGESTREAM_NAME=${params.MBS_FRONTEND_IMAGESTREAM_NAME}",
                '-p', "MBS_FRONTEND_IMAGESTREAM_NAMESPACE=${env.PIPELINE_ID}",
                '-p', "MBS_IMAGE_TAG=${env.TEMP_TAG}",
                '-p', "MBS_BACKEND_IMAGESTREAM_NAME=${params.MBS_BACKEND_IMAGESTREAM_NAME}",
                '-p', "MBS_BACKEND_IMAGESTREAM_NAMESPACE=${env.PIPELINE_ID}",
                '-p', "CREATED=${created}"
              )
              def build = c3i.buildAndWait(script: this, objs: processed, '--from-dir=.')
              def ocpBuild = build.object()
              env.FRONTEND_IMAGE_DIGEST = ocpBuild.status.output.to.imageDigest
              def ref = ocpBuild.status.outputDockerImageReference
              def repo = ref.tokenize(':')[0..-2].join(':')
              env.FRONTEND_IMAGE_REPO = repo
              env.FRONTEND_IMAGE_REF = repo + '@' + env.FRONTEND_IMAGE_DIGEST
              env.FRONTEND_IMAGE_TAG = env.TEMP_TAG
              echo "Built image ${env.FRONTEND_IMAGE_REF}, digest: ${env.FRONTEND_IMAGE_DIGEST}, tag: ${env.FRONTEND_IMAGE_TAG}"
            }
          }
        }
      }
      post {
        failure {
          echo "Failed to build mbs-frontend image ${env.TEMP_TAG}."
        }
        cleanup {
          script {
            if (!env.C3IAAS_NAMESPACE) {
              openshift.withCluster() {
                echo 'Tearing down...'
                openshift.selector('bc', [
                  'app': env.FRONTEND_BUILDCONFIG_ID,
                  'template': 'mbs-frontend-build-template',
                  ]).delete()
              }
            }
          }
        }
      }
    }
    {% include "snippets/mbs-integration-test.groovy" %}
    stage('Push images') {
      when {
        expression {
          return params.FORCE_PUBLISH_IMAGE == 'true' ||
            params.MBS_GIT_REF == params.MBS_MAIN_BRANCH
        }
      }
      steps {
        script {
          if (params.CONTAINER_REGISTRY_CREDENTIALS) {
            dir ("${env.HOME}/.docker") {
              openshift.withCluster() {
                def dockerconf = openshift.selector('secret', params.CONTAINER_REGISTRY_CREDENTIALS).object().data['.dockerconfigjson']
                writeFile file: 'config.json', text: dockerconf, encoding: "Base64"
              }
            }
          }
          def registryToken = readFile(file: '/run/secrets/kubernetes.io/serviceaccount/token')
          def copyDown = { name, src ->
            src = "docker://${src}"
            echo "Pulling ${name} from ${src}..."
            withEnv(["SOURCE_IMAGE_REF=${src}", "TOKEN=${registryToken}"]) {
              sh """
                set -e +x # hide the token from Jenkins console
                mkdir -p _images/${name}
                skopeo copy \
                  --src-cert-dir=/run/secrets/kubernetes.io/serviceaccount/ \
                  --src-creds=serviceaccount:"$TOKEN" \
                  "$SOURCE_IMAGE_REF" dir:_images/${name}
              """
            }
          }
          def pullJobs = [
            'Pulling mbs-backend'  : { copyDown('mbs-backend', env.BACKEND_IMAGE_REF) },
            'Pulling mbs-frontend' : { copyDown('mbs-frontend', env.FRONTEND_IMAGE_REF) }
          ]
          parallel pullJobs
          def copyUp = { name, dest ->
            dest = "${dest}:${params.MBS_DEV_IMAGE_TAG ?: 'latest'}"
            if (!dest.startsWith('atomic:') && !dest.startsWith('docker://')) {
              dest = "docker://${dest}"
            }
            echo "Pushing ${name} to ${dest}..."
            withEnv(["DEST_IMAGE_REF=${dest}"]) {
              retry(5) {
                sh """
                  skopeo copy dir:_images/${name} "$DEST_IMAGE_REF"
                """
              }
            }
          }
          def backendDests = params.MBS_BACKEND_DEV_IMAGE_DESTINATIONS ?
                             params.MBS_BACKEND_DEV_IMAGE_DESTINATIONS.split(',') : []
          def backendPushJobs = backendDests.collectEntries {
            [ "Pushing mbs-backend to ${it}"  : { copyUp('mbs-backend', it) } ]
          }
          parallel backendPushJobs
          // Run all the frontend push jobs after the backend push jobs, so we can trigger
          // on the frontend repo being updated and be confident it is in sync with the
          // backend repo.
          def frontendDests = params.MBS_FRONTEND_DEV_IMAGE_DESTINATIONS ?
                              params.MBS_FRONTEND_DEV_IMAGE_DESTINATIONS.split(',') : []
          def frontendPushJobs = frontendDests.collectEntries {
            [ "Pushing mbs-frontend to ${it}" : { copyUp('mbs-frontend', it) } ]
          }
          parallel frontendPushJobs
        }
      }
      post {
        failure {
          echo 'Pushing images FAILED'
        }
      }
    }
    stage('Tag into ImageStreams') {
      when {
        expression {
          return "${params.MBS_DEV_IMAGE_TAG}" && params.TAG_INTO_IMAGESTREAM == "true" &&
            (params.FORCE_PUBLISH_IMAGE == "true" || params.MBS_GIT_REF == params.MBS_MAIN_BRANCH)
        }
      }
      steps {
        script {
          openshift.withCluster() {
            openshift.withProject(params.MBS_BACKEND_IMAGESTREAM_NAMESPACE ?: env.PIPELINE_ID) {
              def sourceRef = "${params.MBS_BACKEND_IMAGESTREAM_NAME}@${env.BACKEND_IMAGE_DIGEST}"
              def destRef = "${params.MBS_BACKEND_IMAGESTREAM_NAME}:${params.MBS_DEV_IMAGE_TAG}"
              echo "Tagging ${sourceRef} as ${destRef}..."
              openshift.tag(sourceRef, destRef)
            }
            openshift.withProject(params.MBS_FRONTEND_IMAGESTREAM_NAMESPACE ?: env.PIPELINE_ID) {
              def sourceRef = "${params.MBS_FRONTEND_IMAGESTREAM_NAME}@${env.FRONTEND_IMAGE_DIGEST}"
              def destRef = "${params.MBS_FRONTEND_IMAGESTREAM_NAME}:${params.MBS_DEV_IMAGE_TAG}"
              echo "Tagging ${sourceRef} as ${destRef}..."
              openshift.tag(sourceRef, destRef)
            }
          }
        }
      }
      post {
        failure {
          echo "Tagging images as :${params.MBS_DEV_IMAGE_TAG} FAILED"
        }
      }
    }
  }
  post {
    cleanup {
      script {
        if (params.CLEANUP == 'true' && params.USE_C3IAAS != 'true') {
          openshift.withCluster() {
            openshift.withProject(env.PIPELINE_ID) {
              if (env.BACKEND_IMAGE_TAG) {
                echo "Removing tag ${env.BACKEND_IMAGE_TAG} from the ${params.MBS_BACKEND_IMAGESTREAM_NAME} ImageStream..."
                openshift.withProject(params.MBS_BACKEND_IMAGESTREAM_NAMESPACE ?: env.PIPELINE_ID) {
                  openshift.tag("${params.MBS_BACKEND_IMAGESTREAM_NAME}:${env.BACKEND_IMAGE_TAG}", "-d")
                }
              }
              if (env.FRONTEND_IMAGE_TAG) {
                echo "Removing tag ${env.FRONTEND_IMAGE_TAG} from the ${params.MBS_FRONTEND_IMAGESTREAM_NAME} ImageStream..."
                openshift.withProject(params.BS_FRONTEND_IMAGESTREAM_NAMESPACE ?: env.PIPELINE_ID) {
                  openshift.tag("${params.MBS_FRONTEND_IMAGESTREAM_NAME}:${env.FRONTEND_IMAGE_TAG}", "-d")
                }
              }
            }
          }
        }
      }
    }
    success {
      script {
        // on pre-merge workflow success
        if (params.PAGURE_API_KEY_SECRET_NAME && env.PR_NO) {
          try {
            pagure.setBuildStatusOnPR(100, "Build #${env.BUILD_NUMBER} successful (commit: ${env.MBS_GIT_COMMIT.take(8)})")
            echo "Updated PR #${env.PR_NO} status to PASS."
          } catch (e) {
            echo "Error updating PR #${env.PR_NO} status to PASS: ${e}"
          }
        }
        // on post-merge workflow success
        if (params.PAGURE_API_KEY_SECRET_NAME && !env.PR_NO) {
          try {
            pagure.flagCommit('success', 100, "Build #${env.BUILD_NUMBER} successful (commit: ${env.MBS_GIT_COMMIT.take(8)})")
            echo "Updated commit ${env.MBS_GIT_COMMIT} status to PASS."
          } catch (e) {
            echo "Error updating commit ${env.MBS_GIT_COMMIT} status to PASS: ${e}"
          }
        }
      }
    }
    failure {
      script {
        // on pre-merge workflow failure
        if (params.PAGURE_API_KEY_SECRET_NAME && env.PR_NO) {
          // updating Pagure PR flag
          try {
            pagure.setBuildStatusOnPR(0, "Build #${env.BUILD_NUMBER} failed (commit: ${env.MBS_GIT_COMMIT.take(8)})")
            echo "Updated PR #${env.PR_NO} status to FAILURE."
          } catch (e) {
            echo "Error updating PR #${env.PR_NO} status to FAILURE: ${e}"
          }
          // making a comment
          try {
            pagure.commentOnPR("""
            Build #${env.BUILD_NUMBER} [failed](${env.BUILD_URL}) (commit: ${env.MBS_GIT_COMMIT}).
            Rebase or make new commits to rebuild.
            """.stripIndent(), env.PR_NO)
            echo "Comment made."
          } catch (e) {
            echo "Error making a comment on PR #${env.PR_NO}: ${e}"
          }
        }
        // on post-merge workflow failure
        if (!env.PR_NO) {
          // updating Pagure commit flag
          if (params.PAGURE_API_KEY_SECRET_NAME) {
            try {
              pagure.flagCommit('failure', 0, "Build #${env.BUILD_NUMBER} failed (commit: ${env.MBS_GIT_COMMIT.take(8)})")
              echo "Updated commit ${env.MBS_GIT_COMMIT} status to FAILURE."
            } catch (e) {
              echo "Error updating commit ${env.MBS_GIT_COMMIT} status to FAILURE: ${e}"
            }
          }
          // sending email
          if (params.MAIL_ADDRESS){
            try {
              sendBuildStatusEmail('failed')
            } catch (e) {
              echo "Error sending email: ${e}"
            }
          }
        }
      }
    }
  }
}
def getPrNo(branch) {
  def prMatch = branch =~ /^(?:.+\/)?pull\/(\d+)\/head$/
  return prMatch ? prMatch[0][1] : ''
}
def sendBuildStatusEmail(String status) {
  def recipient = params.MAIL_ADDRESS
  def subject = "Jenkins job ${env.JOB_NAME} #${env.BUILD_NUMBER} ${status}."
  def body = "Build URL: ${env.BUILD_URL}"
  if (env.PR_NO) {
    subject = "Jenkins job ${env.JOB_NAME}, PR #${env.PR_NO} ${status}."
    body += "\nPull Request: ${env.PR_URL}"
  }
  emailext to: recipient, subject: subject, body: body
}
{% include "snippets/functions.groovy" %}

import datetime
from filecmp import dircmp

from datetime import datetime
from . import common
import os
import pprint
import shutil
import tempfile
from distutils import dir_util

# re-use existing CF build logic
from . import check

argparser = None

# all options passed to javac by the build system are copied to the invocations
# of javac that run the Checker Framework, except those that either exactly match
# an element of ignored_options or start with an element of ignored_options_prefixes
ignored_options = ("classpath",
                  "nowarn", "Xmaxerrs", "Xmaxwarns", "Werror",
                  "processorpath", "processor", "proc:none",
                  "XepDisableAllChecks", "Xplugin:ErrorProne")
ignored_options_prefixes = ("Xep:", "XepExcludedPaths:")

def run(args, javac_commands, jars):
    # checker-framework javac.
    javacheck = os.environ['CHECKERFRAMEWORK']+"/checker/bin/javac"
    checker_command = [javacheck, "-Xmaxerrs", "10000", "-Xmaxwarns", "10000"]
    if args.checker is not None:
        processorArg = ["-processor", args.checker]
    else:
        # checker should run via auto-discovery
        processorArg = []

    if args.jdkVersion is not None:
        jdkVersion = int(args.jdkVersion)
    else:
        jdkVersion = 8

    if args.extraJavacArgs is not None:
        checker_command += args.extraJavacArgs.split()
    for i in range(len(checker_command)):
        if checker_command[i] == '""' or checker_command[i] == '""':
            checker_command[i] = ''

    for jc in javac_commands:

        # something searchable to delineate different javac commands
        common.run_cmd(["true", "=== Beginning of WPI run for extracted javac command: " + str(jc) +"  ==="], args, "wpi")

        wpiDir = os.path.join(os.getcwd(), 'build/whole-program-inference')
        # if there is already a WPI directory, delete it and start over
        if os.path.isdir(wpiDir):
            shutil.rmtree(wpiDir)

        iteration = 0
        diffResult = True
        ajavaDirs = []
        resultsDir = tempfile.mkdtemp(prefix="wpi-ajava-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-")

        print("Directory for generated annotation files: " + str(resultsDir))

        javac_switches = jc['javac_switches']
        cp = javac_switches['classpath']
        if 'processor' in javac_switches and len(processorArg) == 2:
            processorArg[1] += "," + javac_switches['processor']

        java_files = jc['java_files']

        # delombok
        delombok = False
        jars = cp.split(":")
        lombokjar = ""
        for jar in jars:
            # This should catch only the Lombok jar, because it's based
            # on Lombok's Maven coordinates. First is the Maven repo file structure;
            # second is the gradle cache's file structure.
            lombok_dirs = ["/org/projectlombok/lombok/", "/org.projectlombok/lombok/"]
            if any([x in jar for x in lombok_dirs]):
                lombokjar = jar
                break

        # must wait until here to supply the classpath without lombok
        if lombokjar != "":
            # delombok takes a directory as input rather than individual source files,
            # so this guesses at what the correct top-level directory is. It's a hack,
            # but it should work for Maven and Gradle projects that follow the default
            # conventions. For compilation to make sense, there must be at least one
            # Java file, so this access should be safe.
            anySrcFile = java_files[0]
            standardSrcDir = "src/main/java/"

            standardSrcIndex = anySrcFile.index(standardSrcDir)

            if standardSrcDir != -1:
                srcDir = anySrcFile[:standardSrcIndex]
                lombok_cmd = ["java", "-jar", lombokjar, "delombok",
                              srcDir + "/src/main/java/", "-d", srcDir + "/delombok/main/java",
                              "-c", cp]
                common.run_cmd(lombok_cmd, args, "wpi")
                # replace the original source files with the delombok'd code, so that
                # the actual javac commands don't need to be modified
                dir_util.copy_tree(srcDir + "/delombok/", srcDir + "/src/")

                # for modifying the checker command in each iteration
                delombok = True



        # include processor path in the class path if it is present
        pp = ''
        if 'processorpath' in javac_switches:
            pp = javac_switches['processorpath'] + ':'
        if args.quals:
            cp += args.quals + ':'
        if args.lib_dir:
            cp += pp + args.lib_dir + ':'

        release8 = False
        other_args = []
        for k, v in list(javac_switches.items()):
            if k not in ignored_options and not k.startswith(ignored_options_prefixes):
                if k == "source" or k == "target" or k == "-release":
                    # If the source/target is < 8, change it to 8.
                    # The CF is generally incompatible with java versions below 8, so
                    # this tries treating the code as Java 8 source. If it doesn't work,
                    # that's okay - there is no guarantee that DLJC will faithfully reproduce
                    # the build, and this is the best that DLJC can do in this situation.
                    if v in ["1.5", "5", "1.6", "6", "1.7", "7", "1.8"]:
                        v = "8"
                    if v == "8":
                        release8 = True
                    # Do not use source/target, because Java 11 JVMs will
                    # crash on some classes, e.g.
                    # https://bugs.openjdk.java.net/browse/JDK-8212636.
                    # Use --release instead.
                    if jdkVersion == 11:
                        k = "-release"
                    elif jdkVersion == 8 and k == "-release":
                        # don't try to use --release on a Java 8 JVM, which doesn't support it
                        v = False
                # Combine --add-opens into a single arg with equals, so we
                # can more easily remove key and value for release8, below:
                if v is not None and v is not True and k.startswith("-add-opens"):
                    other_args.append("-" + k + "=" + v)
                else: 
                    if v is None or v is not False:
                        other_args.append("-" + k)
                    if v is not None and v is not True:
                        if v == '""' or v == "''":
                            v = ''
                        other_args.append(str(v))

        checker_command += check.getArgumentsByVersion(jdkVersion, other_args)

        if release8:
            # Avoid javac "error: option --add-opens not allowed with target 1.8"
            checker_command = [arg for arg in checker_command if not arg.startswith("--add-opens")]
            other_args = [arg for arg in other_args if not arg.startswith("--add-opens")]

        while diffResult:
            # This is definitely hacky. "true" is a bash no-op, so this command actually does nothing. However, it gets
            # written into the log of the commands that WPI has executed, so basically it's a comment in that log.
            # That log is controlled by logic inside common.run_cmd, so it can't be written to directly from here.
            common.run_cmd(["true", "=== Starting WPI iteration " + str(iteration) + ": ==="], args, "wpi")
            iterationCheckerCmd = checker_command.copy()
            # TODO: the switch to ajava files instead of stub files should make the separate stubs argument
            # to dljc unnecessary, as there's no longer any need to combine stub lists.
            # TODO: do we need to treat the -Aajava argument the same way? I.e., will this work if the user
            # supplies their own -Aajava= argument as part of the extraJavacArgs argument?
            if args.stubs:
                iterationCheckerCmd.append("-Astubs=" + str(args.stubs))
            # only use the last element in ajavaDirs, to avoid an ambiguity warning that would be issued
            # if two ajava files for the same source file were to be passed
            iterationAjavaDirs = [ajavaDirs[-1]] if ajavaDirs else []
            if args.ajava:
                iterationAjavaDirs.append(str(args.ajava))
            if iterationAjavaDirs:
                iterationCheckerCmd.append(
                    "-Aajava=" + ":".join(iterationAjavaDirs))

            # suppress all type.anno.before.modifier warnings, because delombok
            # prints annotations in the wrong place
            if delombok:
                iterationCheckerCmd.append("-AsuppressWarnings=type.anno.before.modifier")

            pprint.pformat(jc)

            cmd = iterationCheckerCmd + ["-classpath", cp] + processorArg + other_args + java_files
            stats = common.run_cmd(cmd + ["-Ainfer=ajava", "-Awarns"], args, 'wpi')

            # process outputs
            # move the old wpi files, add them to ajava path
            previousIterationDir = os.path.join(resultsDir, "iteration" + str(iteration))
            os.mkdir(previousIterationDir)
            iteration += 1
            try:
                ajavaFiles = os.listdir(wpiDir)
            except OSError as e:
                print("No WPI outputs were discovered; it is likely that WPI failed or the Checker Framework crashed.")
                print("Check the file " + os.path.join(os.getcwd(), 'dljc-out', 'wpi.log') + " for more information.")
                raise e

            for ajavaFile in ajavaFiles:
                shutil.move(os.path.join(wpiDir, ajavaFile),
                            previousIterationDir)

            ajavaDirs.append(previousIterationDir)

            if len(ajavaDirs) > 1:
                dcmp = dircmp(ajavaDirs[-1], ajavaDirs[-2])
                diffResult = has_differing_files(dcmp)

        # Run one final time without "-Awarns", for the final user output.
        common.run_cmd(["true", "=== WPI finished. Output of final typechecking run: ==="], args, "wpi")
        common.run_cmd(cmd, args, 'wpi')


def has_differing_files(dcmp):
    return (dcmp.diff_files
            or any(map(has_differing_files, dcmp.subdirs.values())))
